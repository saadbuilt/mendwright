# mendwright guide

The [README](../README.md) gets you to a first heal.
This page covers the rest: settings, providers, the pytest plugin, async and Selenium details, the files mendwright writes, CI, the command line and troubleshooting.

Contents:

- [Configuration](#configuration)
- [Heals without a model](#heals-without-a-model)
- [No changes per step](#no-changes-per-step)
- [get_by locators](#get_by-locators)
- [Describing an element with intent=](#describing-an-element-with-intent)
- [Checking what a read returns](#checking-what-a-read-returns)
- [Lists](#lists)
- [Providers](#providers)
- [pytest plugin](#pytest-plugin)
- [Async Playwright](#async-playwright)
- [Selenium](#selenium)
- [Iframes](#iframes)
- [Layout changes](#layout-changes)
- [Flow changes](#flow-changes)
- [Selectors](#selectors)
- [Roles](#roles)
- [Destructive controls](#destructive-controls)
- [Store and journal](#store-and-journal)
- [CI](#ci)
- [Checking for drift before a run](#checking-for-drift-before-a-run)
- [Logging](#logging)
- [Command line](#command-line)
- [Low-level API](#low-level-api)
- [Troubleshooting](#troubleshooting)

## Configuration

### mendwright.open()

`mendwright.open(page, **settings)` wraps a page or driver and returns a `Healer`.
Every setting is a keyword argument. An argument you pass wins over the matching environment variable.

| Argument | Default | Meaning |
|---|---|---|
| `page` | required | A sync Playwright `Page` or `Frame` (patchright and camoufox too), a Selenium WebDriver, or a driver you registered with `register_driver` |
| `provider` | `auto_provider()` | The model provider. If none can be built, selectors still resolve, only [fingerprint heals](#heals-without-a-model) happen, other heals are declined with the reason, and one WARNING is logged. |
| `fingerprint` | `MENDWRIGHT_FINGERPRINT`, else on | Look for the element from its saved fingerprint before any model call. `False` turns it off. See [Heals without a model](#heals-without-a-model). |
| `store_path` | `MENDWRIGHT_STORE`, else `./.mendwright/overrides.json` | Where heals are saved. `~` is expanded. Relative paths are relative to the current directory. |
| `store` | built from `store_path` | A `SelectorStore`, for example from `load_store()` with a seed file |
| `timeout_ms` | `MENDWRIGHT_TIMEOUT_MS`, else 10000 | How long `locator()` polls before it treats a selector as broken |
| `dry_run` | `MENDWRIGHT_DRY_RUN`, else off | Ask for heals and check each proposal against the captured HTML only (no live-page check), but save nothing and raise `AttentionRequired` |
| `destructive_keys` | none | Keys that are never healed. Nothing can unmark them later. |
| `guard` | always allows | A callable. While it returns False, heals are declined, for example while a payment is in flight. |
| `rate_per_key_per_hour` | 3 | Heal attempts per key per hour for a direct provider (rejected and dry-run attempts count, and each attempt makes up to 2 model calls). `None` removes the limit. |
| `rate_global_per_hour` | 60 | Heal attempts per hour across all keys for a direct provider. `None` removes the limit. |
| `guardrails` | built from the two rates | A `mendwright.limits.Guardrails`, for example with a `SpendCap(max_calls=...)` added |
| `journal` | `journal.jsonl` next to the store | A `mendwright.journal.Journal`, or `NullJournal()` to write none |
| `cookies` | `None` | `"reject"` or `"accept"` registers an interruption for the consent banners of common consent managers, under the key `cookie_banner`. `None` clicks nothing you didn't ask for. See [Cookie banners](#cookie-banners). |
| `heal_across_pages` | `False` | `True` lets a key heal on a page where it was never found. See [Steps that moved to another page](#steps-that-moved-to-another-page). |
| `dismiss` | `False` | `True` dismisses a popup nobody registered, such as a newsletter modal, when it covers the element of an action. `False` makes no browser call for it. See [Popups mendwright wasn't told about](#popups-mendwright-wasnt-told-about). |
| `expect_values` | none | `{key: check}`: what a read of each key must return when it goes through a heal, as `expect_value=` of `heal.locator()` says, also for the lookups of `heal.page` and `heal.driver`, whose key is the selector (`by=value` on `heal.driver`). See [Checking what a read returns](#checking-what-a-read-returns). |
| `heal_get_by` | `MENDWRIGHT_HEAL_GET_BY`, else off | `True` looks up and heals `heal.page.get_by_role()` and the other `get_by_*` calls too, from their fingerprints only. On Selenium it does nothing. See [get_by locators](#get_by-locators). |

The two rates are shared by every Healer in one process that uses the same store file and the same two rate values.
`GatewayProvider` gets no client-side limit, because the gateway meters on its side.

### Healer.locator()

```python
heal.locator(key, selector=None, *, role=None, text=None, destructive=None,
             timeout_ms=None, poll_ms=200, optional=False, intent=None, expect_value=None)
```

| Argument | Meaning |
|---|---|
| `key` | The name the element's selectors and heals are saved under. Use one key per element. |
| `selector` | What your code says the element is: an XPath, CSS or Playwright selector, or a list of them tried in order. Leave it out to use only what the store has. |
| `role` | The kind of element (see [Roles](#roles)). Optional: mendwright saves the role the first time a selector matches. |
| `text` | The element's expected visible text, a string or a list. The model gets it as a hint. |
| `destructive` | `True` means never heal this key, and is saved for later runs. `False` clears a flag saved that way. `None` keeps what was saved. See [Destructive controls](#destructive-controls). |
| `timeout_ms` | Overrides the Healer's timeout for this call |
| `poll_ms` | How often to poll while waiting |
| `optional` | `True` means the step may not be there. If the element doesn't appear, it isn't healed, and the proxy's `.present` is False. See [Optional steps](#optional-steps). |
| `intent` | What the element is for, in words, such as `"the terms checkbox in the signup form"`. It is saved with the key, and the model gets it when it heals the key. With `role=` and no selector, a model looks for the element on first use. See [Describing an element with intent=](#describing-an-element-with-intent). |
| `expect_value` | What a read of the element must return when it was found through a heal: `"money"`, `"number"`, `"integer"`, `"percent"`, `"date"`, `"email"` or `"url"`, a compiled regular expression, or a callable. A value that fails quarantines the heal, and the read raises `AttentionRequired`. See [Checking what a read returns](#checking-what-a-read-returns). |

It returns an `ActionProxy` once the element is found, healed or not, or a proxy with `.present` False for a step that was skipped.
It raises `AttentionRequired` when the element can't be found or healed, `InvalidSelector` or `TypeError` for a selector that can never work (or an `intent=` that is not a non-empty string), and `ValueError` for an unknown role or for `intent=` with no selector and no role. An `expect_value=` that is none of those raises `TypeError` or `ValueError` before anything touches the page.

Other `Healer` members:

- `heal.interrupt(key, selector, *, action=None, args=(), role=None, run=None)`: registers a step that can show up at any point. See [Interruptions](#interruptions), and [Interruptions that need a form](#interruptions-that-need-a-form) for `run=`.
- `heal.handled_interruptions`: the interruptions this Healer handled, as `InterruptionEvent(key, before)`. With `dismiss=True`, the popups it dismissed are listed too, under the key `<popup>`, with `popup` (how the log line names it), `how` (`"escape"`, `"close"` or `"decline"`) and `label` (the button's) set (see [Popups mendwright wasn't told about](#popups-mendwright-wasnt-told-about)).
- `heal.present(key)`: True if a stored selector matches one element at the moment of the call. It doesn't wait or heal.
- `heal.locator_all(key, selector=None, *, role=None, timeout_ms=None, poll_ms=200, optional=False, nonempty=False)`: the elements of a list, such as the cards of a product grid. It gives only what your own selectors match. With `nonempty=True`, a list whose selectors match nothing is looked for and proposed to you. See [Lists](#lists).
- `heal.page` and `heal.driver`: the page or driver to use in place of yours, so steps heal without changing them. See [No changes per step](#no-changes-per-step).
- `heal.heals`: the heals this Healer applied, as `HealEvent(key, old, new, confirmed, source)`. `source` is `"fingerprint"` (no model call), `"model"`, `"intent"` for a key that had no selector and was found from its `intent=`, or `"frame"` for a key whose own selector matched only inside an iframe (see [Iframes](#iframes)).
- `heal.pending()`: `{key: xpath}` for heals that aren't confirmed yet.
- `heal.confirm_all(True)` or `heal.confirm_all(False)`: confirm or quarantine every pending heal.

### Actions and confirmation

The proxy runs these actions through the driver: `click`, `dblclick`, `fill`, `type`, `press`, `press_sequentially`, `check`, `uncheck`, `set_checked`, `select_option`, `hover`, `tap`, `clear`, `focus`, `set_input_files`, `drag_to`, `send_keys` and `submit`.
Keyword arguments pass through, and the return value is what the native call returned.
Anything else, such as `inner_text()`, `get_attribute()` or `is_checked()`, goes to the native object unchanged. On Playwright, the proxy's first read (`text_content()`, `inner_text()`, `inner_html()`, `get_attribute()`, `input_value()`, `is_visible()`, `is_hidden()`, `is_checked()`, `is_enabled()`, `is_disabled()` or `is_editable()`) first handles the registered interruptions the page shows, as an action does, unless one of its actions came first. Its later reads don't look again (see [Interruptions](#interruptions)). With `expect_value=`, a read of an element found through a heal is checked first (see [Checking what a read returns](#checking-what-a-read-returns)).
`proxy.native` is the Playwright `Locator` or Selenium `WebElement` itself, for APIs that check the type.
`proxy.present` is False for a skipped step; its actions then do nothing and return `None`:

```python
from playwright.sync_api import expect

terms = heal.locator("accept_terms", "//input[@id='terms']", role="checkbox")
terms.check(expect=terms.is_checked)
expect(terms.native).to_be_checked()
```

A new heal starts unconfirmed. It is confirmed when:

- the `expect=` callable on an action returns something truthy
- a read returns a value that passes `expect_value=`
- you call `proxy.confirm(True)` or `heal.confirm_all(True)`
- the pytest plugin sees the test pass

It is quarantined when the action raises in the browser, when `expect=` returns something falsy or raises, when a read returns a value that fails `expect_value=`, or when you call `confirm(False)`.
A quarantined selector stays in the file with status `quarantined`. It isn't tried again, and a later proposal for that key is rejected if it reaches the same element, even through a different XPath. That counts only in the same place: a selector quarantined inside an iframe doesn't block an element that looks the same on the page, and the other way round. (The exact same selector text stays refused in any iframe, because the store keeps one row per text.)
`AttributeError`, `TypeError` and `ValueError` from a wrong verb or argument quarantine nothing.

Confirming writes a `heal.canary` line to the journal and calls the provider's `report_outcome()`. The gateway uses that to record outcomes.
An unconfirmed heal stays active and is used on later runs.

### Environment variables

| Variable | Meaning |
|---|---|
| `MENDWRIGHT_STORE` | Store path, the default for `store_path` |
| `MENDWRIGHT_TIMEOUT_MS` | Default `timeout_ms`, in whole milliseconds |
| `MENDWRIGHT_DRY_RUN` | `1` turns dry run on |
| `MENDWRIGHT_DISABLE` | `1` resolves selectors but never heals or calls a provider, even one passed as `provider=`. The fingerprint tier is off too. |
| `MENDWRIGHT_FINGERPRINT` | `0` turns the fingerprint tier off, the default for `fingerprint=` |
| `MENDWRIGHT_HEAL_GET_BY` | `1` heals `heal.page.get_by_*` calls, the default for `heal_get_by=` (see [get_by locators](#get_by-locators)) |
| `MENDWRIGHT_PROVIDER` | `anthropic`, `openai`, `gemini`, `gateway`, `local` or `ollama`: use that provider instead of detecting one |
| `MENDWRIGHT_GATEWAY` | Gateway URL, which selects `GatewayProvider` |
| `MENDWRIGHT_LOCAL_MODEL` | The Ollama name of mendwright's local selector model, which selects `LocalModelProvider` |
| `MENDWRIGHT_LOCAL_MIN_CONFIDENCE` | The least probability a pick of the local model needs to be used, from 0 to 1 (default 0.8, 0 turns the check off) |
| `MENDWRIGHT_KEY` | The client key the gateway checks |
| `MENDWRIGHT_AUTH_HEADER` | The header that carries it, `X-Heal-Key` by default |
| `MENDWRIGHT_REQUIRE_ENGINES` | Only for this repo's browser tests: comma-separated engine ids that must start, so CI fails instead of skipping |

Flags take `1`, `true`, `yes` or `on`, and `0`, `false`, `no`, `off` or empty.
Any other value makes `open()` raise `ValueError` (for `MENDWRIGHT_DRY_RUN`, only when `dry_run=` isn't passed, and for `MENDWRIGHT_HEAL_GET_BY`, only when `heal_get_by=` isn't).

## Heals without a model

mendwright heals many broken selectors with no model and no API key.
Each time a key's selector matches, it saves a fingerprint of the element in `overrides.json`. When the selector breaks, mendwright looks for that element on the page first, and asks a model only when that match isn't confident.

### What a fingerprint holds

- the tag, and the attributes that tend to survive a redesign: `id`, `name`, `type`, `role`, the test ids (`data-testid`, `data-test`, `data-qa`), `aria-label`, `placeholder`, `title`, `alt`, `autocomplete`, and the path of `href` (no host, query or fragment)
- the class names that don't look generated. `css-1x2y3`, `Header_link__a1B2c` and state words such as `active` are left out.
- the visible text, or for a form field the text of its label, with spaces collapsed, cut to 80 characters, and runs of 12 to 19 digits replaced by `#`
- the tag, id, classes and role of up to 4 ancestors

It never reads an input's value.
It is read in one round trip after the selector matches, once per key and selector per Healer, and the store file is written only when the fingerprint changed.
It isn't recorded for a key marked destructive, a skipped step, or a heal made in this run that isn't confirmed yet. A match inside an iframe is read inside the iframe.
In a later run it is refreshed through whichever selector matches, so after a heal it describes the healed element, even one that was never confirmed. Check unconfirmed heals with `mendwright report`.

### When it heals

When every selector of a key misses, mendwright scores each element of the page that has the key's role against the fingerprint, from 0 to 1.
It heals only when all of these hold:

- The best score is at least 0.40.
- No other visible element scores within 0.12 of it.
- The best element shares a strong sign of identity with the recorded one: the same id (not a generated one), test id, name, link path or `autocomplete` value, or nearly the same text, label or aria-label. Nearly the same means the same numbers and the same words, with a typo, a plural, a filler word such as "the", or a change in spacing allowed. "Delete file 1" and "Delete file 2" are not the same, and neither are "Delete this file" and "Delete this site".
- It sits inside every recorded ancestor whose id is still on the page. A Save button that left its form, while the form is still there, is not taken.
- A unique XPath without positions can be built for it.

That XPath then goes through the same checks as a model's proposal (see [How it works](../README.md#how-it-works)): one match of the step's role in the captured HTML, not quarantined, and one visible match on the live page that still has the role.

Everything that stops a heal before a model call stops it before the fingerprint too: a key marked destructive, a page where the key was never found (the page guard), an unknown role, and a `guard=` that returns False.
Interruptions registered with `heal.interrupt()` are handled first.
Dry run checks the match and applies nothing.
A fingerprint heal doesn't count toward the hourly limit, and it isn't reported to the provider, which didn't propose it.

### With and without a provider

| Provider | Fingerprint match | What happens |
|---|---|---|
| none | confident | healed, no model call |
| none | not confident, or no fingerprint yet | `AttentionRequired`, saying why and which variables to set for a model |
| configured | confident | healed, no model call |
| configured | not confident, or no fingerprint yet | the model is asked |

A key that never matched has no fingerprint, so a selector that is already broken the first time mendwright sees it needs a model.

`fingerprint=False` in `mendwright.open()` or `MENDWRIGHT_FINGERPRINT=0` turns the tier off, and `MENDWRIGHT_DISABLE=1` turns it off with every other heal.
`HealCoordinator` in the low-level API leaves it off unless you pass `fingerprint=True`.
A list of elements, such as the cards of a product grid, has a list fingerprint of its own, recorded in the same way, but a list is never healed: with `nonempty=True`, mendwright looks for a list whose selector broke and proposes the new selector for you to check (see [Lists that always have items](#lists-that-always-have-items-nonemptytrue)).

### What you see

A fingerprint heal logs one line:

```text
mendwright: healed 'terms': #accept-terms -> //input[@data-testid='legal-consent'] (fingerprint, 0.0s)
```

In the journal, its `heal.proposed` has `"source": "fingerprint"` and a reasoning such as `"fingerprint score 0.83, next best 0.41"`, and its `heal.applied` has `"provider": "fingerprint"` and `"source": "fingerprint"`.
`heal.heals` lists it with `source="fingerprint"`.

When the tier doesn't heal, the error says why, for example:

- `'terms' has no fingerprint yet (mendwright records one each time its selector matches)`
- `the fingerprint match for 'terms' was not confident: two elements score alike (0.52 and 0.52), so the fingerprint does not tell them apart`
- `the fingerprint match for 'terms' was not confident: the best match (score 0.61) shares no id, test id, name, link, autocomplete value, label or text with the element that was recorded`
- `the fingerprint match for 'save' was not confident: the best match is outside the element with id 'profile-form', which held the recorded element and is still on the page`

With no provider, the message goes on with `and no model provider is configured to try instead.` and the variables to set.

### How well it works

On the [47-case benchmark](benchmark.md) with no model, the tier healed 20 of 35 broken selectors correctly, healed none onto the wrong element, and declined all 12 cases where no heal was right ([results](../benchmarks/RESULTS.md)).
Those numbers are in-sample: its thresholds were tuned on the same 47 cases.
Pages written afterwards to probe it did find wrong heals, and the rules were tightened until it declined them.
Expect it to be wrong now and then on your own pages, and mark pay, submit and delete steps with `destructive=True`.
It misses translated text, an element whose tag changed, look-alike rows and cards, and labels that changed a word. A model can still heal those.

## No changes per step

`heal.page` and `heal.driver` stand in for the page or driver your script already uses, so an existing script heals when you change the line that makes it:

```python
page = mendwright.open(page).page          # Playwright
page.fill("#email", "ada@example.com")
page.locator("#terms").check()

driver = mendwright.open(driver).driver    # Selenium
driver.find_element(By.ID, "terms").click()
```

With async Playwright, use `(await mendwright.aio.open(page)).page`. Its shortcuts and locator methods are awaited, as on the real page.

### Playwright

`page.locator(selector)` and the page methods that take a selector go through `heal.locator(selector, selector)`, so the selector is also the key its heals are saved under.

- The locator actions heal: `click`, `fill`, `check`, `select_option`, `press`, `hover` and the rest of the list in [Actions and confirmation](#actions-and-confirmation). So do the page shortcuts `click`, `dblclick`, `fill`, `check`, `uncheck`, `select_option`, `press`, `type` and `hover`.
- The reads heal too: `is_checked`, `text_content`, `inner_text`, `inner_html`, `get_attribute`, `input_value`, `is_enabled`, `is_disabled` and `is_editable` on a locator, and `is_checked`, `text_content`, `inner_text`, `get_attribute` and `input_value` on the page. With `mendwright.open(expect_values={selector: check})`, what `inner_text`, `text_content`, `input_value` and `get_attribute` return through a heal is checked (see [Checking what a read returns](#checking-what-a-read-returns)).
- `locator(selector).all()` gives what Playwright gives, as in 0.7.0. It never waits, heals or saves anything, so a list that matches nothing is `[]` at once, and a loop that waits for toasts to go away works as before. `heal.locator_all()` records a list and raises when it matches nothing (see [Lists](#lists)). With `dismiss=True`, its elements are `GuardedLocator`s.
- Reads, `set_input_files` and `focus` work on hidden elements in Playwright. For these, a selector that matches a hidden element, or several, goes to the real page as written when no saved heal finds the element. Only a selector that matches nothing is healed. The actions that need one visible element heal any selector that doesn't match one.
- `is_visible(selector)` answers at once and never heals. For a selector mendwright has saved, it checks the saved selectors, heals included, without scrolling or writing the store.
- These are not healed and go to the real page: a locator built with options (`has_text=`, `has=`), a chained locator such as `page.locator("form").locator("button")`, `.first` and `.nth()`, `get_by_role()` and the other `get_by_*` locators unless you pass `heal_get_by=True` (see [get_by locators](#get_by-locators)), `wait_for_selector()`, `query_selector()`, `expect(page.locator(...))`, and every other page method (`goto`, `evaluate`, `keyboard`, `frames`, and so on).
- From Playwright 1.42, interruptions are still handled before the actions of those locators (`click`, `fill`, `check`, `hover` and so on) and before `expect(...)`, `wait_for_selector()` and `Locator.wait_for()`, but not before `page.mouse`, `page.keyboard`, `dispatch_event()` or an action with `force=True`: see [Interruptions](#interruptions). With `dismiss=True`, the locators among them come back as guard-only proxies whose actions look for interruptions and popups first: see [Popups mendwright wasn't told about](#popups-mendwright-wasnt-told-about).
- The proxy passes `isinstance()` checks for the page's class, so `expect(page).to_have_title(...)` works. `page.native` is the real page.
- `timeout=` becomes the lookup's timeout. `timeout=0`, which means no limit in Playwright, keeps the Healer's own, so a missing element waits `timeout_ms` (10 seconds by default) before it is healed, not Playwright's 30 seconds.
- `strict=` is dropped, because a mendwright lookup is always strict.

`mendwright.open()` and `mendwright.aio.open()` also take a proxy, and use the real page behind it.
On a Playwright healer, `heal.driver` is still mendwright's own driver object.

### Selenium

`driver.find_element(by, value)` goes through `heal.locator(f"{by}={value}", selector)` for `By.XPATH`, `By.CSS_SELECTOR`, `By.ID` and `By.NAME`, and returns the element as a `HealingElement` (around a `FramedElement` inside an iframe).
`By.ID` and `By.NAME` become `[id="..."]` and `[name="..."]` CSS selectors, and the key for `find_element(By.ID, "terms")` is `id=terms`.

- A selector that matches a hidden element, or several, comes from the real driver as written (the first match, as `find_element` always returned) when no saved heal finds the element. Only a selector that matches nothing is healed.
- When the element can't be found or healed, it raises an `AttentionRequired` that is also Selenium's `NoSuchElementException`, so `except NoSuchElementException` and `WebDriverWait` still work.
- These are not healed: the other `By` values (`LINK_TEXT`, `CLASS_NAME`, `TAG_NAME`, and so on), and `find_element()` or `find_elements()` on a returned element. Their elements come back as `HealingElement`s too. Every other driver method is the real driver's.
- `find_elements()` gives what Selenium gives, as in 0.7.0, with each element a `HealingElement`. It never waits, heals or saves anything, so a list that matches nothing is `[]` at once, and `WebDriverWait` on it works as before. `heal.locator_all()` records a list and raises when it matches nothing (see [Lists](#lists)).
- With `mendwright.open(expect_values={"by=value": check})`, a `HealingElement`'s `text`, `get_attribute()` and `get_dom_attribute()` are checked when `find_element()` found it through a heal (see [Checking what a read returns](#checking-what-a-read-returns)).
- A `HealingElement`'s `click()`, `send_keys()`, `clear()` and `submit()` handle a registered [interruption](#interruptions) that covers the element first, as a proxy action does, and try a click that something covered once more after handling one. Otherwise they raise Selenium's own error. With no interruption registered and `dismiss` off, they make no browser call besides the element's own; with `dismiss=True`, one more, to look for a popup. `ActionChains` actions, and the elements that `execute_script()` or `switch_to.active_element` return, are Selenium's own and get no such check.
- A `HealingElement` passes `isinstance(el, WebElement)`, is equal to the element it wraps (and hashes the same), and works with `driver.execute_script(..., el)`, `ActionChains` and `expected_conditions`. One around a `FramedElement` needs `in_frame()` for those, as before (see [Selenium](#selenium-1)). Everything else is the element's own, and `el.native` is the element.
- A missing element waits `timeout_ms` before it is healed, where Selenium raises at once unless you set an implicit wait. To check that an element is absent, use `driver.find_elements(...)`, which never waits.
- When an action fails in the browser on an element that `find_element()` looked up through `heal.locator()`, a heal made for it is quarantined, as for a proxy action. The actions of the elements of the other `By` values and of `find_elements()` confirm and quarantine nothing. A `StaleElementReferenceException` quarantines nothing either: the page re-rendered the element after it was found, which says nothing about the heal, and a new `find_element()` finds it again. A heal whose actions went through is confirmed by the pytest plugin when the test passes, and `heal.confirm_all(False)` quarantines it.

On a Selenium healer, `heal.page` is still the WebDriver.

## get_by locators

```python
page = mendwright.open(page, heal_get_by=True).page
page.get_by_role("button", name="Sign in").click()   # healed when the button is renamed
```

With `heal_get_by=True`, `get_by_role()`, `get_by_text()`, `get_by_label()`, `get_by_placeholder()`, `get_by_alt_text()`, `get_by_title()` and `get_by_test_id()` on `heal.page` are looked up and healed the way `heal.page.locator(selector)` is.
A call that matches nothing is healed from the fingerprint mendwright recorded when the call last matched, never by a model.

### Turning it on

Pass `heal_get_by=True` to `mendwright.open()` or `mendwright.aio.open()`, set `MENDWRIGHT_HEAL_GET_BY=1` for every healer that doesn't pass it, or run `pytest --mendwright-heal-get-by`, which sets the variable for the run.
It is off by default. Off, `heal.page.get_by_*` are Playwright's own locators, as in 0.8.0 (with `dismiss=True`, `GuardedLocator`s), and nothing about them is saved.
On Selenium the argument is accepted and does nothing: `heal.driver` has no `get_by_*`.

What changes when it is on:

- A call that matches nothing waits `timeout_ms` (10 seconds by default) before it is healed, not Playwright's 30 seconds or a timeout set with `page.set_default_timeout()`, which mendwright can't read. Pass `timeout=` on the action for a slow page.
- When the element can't be found or healed, the error is an `AttentionRequired` that is also Playwright's `TimeoutError` (`mendwright.getby.GetByNotFound`), so `except TimeoutError` still catches it.
- The registered [interruptions](#interruptions) are handled by the step, and logged and journaled before the call's key, such as `before 'get_by_role("button", name="Continue")'`, instead of `'<page action>'`.
- The store gets a row for each call that matched, with its role, its pages and its fingerprint, as for any key.

`MENDWRIGHT_DISABLE=1` leaves the calls looked up, and nothing heals.

### The key

A call is saved under a key written as the call, with its strings in double quotes:

| Call on `heal.page` | Key |
|---|---|
| `get_by_role("button", name="Sign in")` | `get_by_role("button", name="Sign in")` |
| `get_by_role(role="button", name="Sign in", exact=False)` | `get_by_role("button", name="Sign in")` |
| `get_by_role("button", name="Sign in", exact=True)` | `get_by_role("button", name="Sign in", exact=True)` |
| `get_by_role("heading", name="Cart", level=2)` | `get_by_role("heading", name="Cart", level=2)` |
| `get_by_role("button", name=re.compile(r"sign\s?in", re.I))` | `get_by_role("button", name=re.compile("sign\\s?in", re.IGNORECASE))` |
| `get_by_role("navigation")` | `get_by_role("navigation")` |
| `get_by_role("button", exact=True)` | `get_by_role("button")` |
| `get_by_text("Free shipping")` | `get_by_text("Free shipping")` |
| `get_by_text('Say "hi"')` | `get_by_text("Say \"hi\"")` |
| `get_by_label("Email", exact=True)` | `get_by_label("Email", exact=True)` |
| `get_by_placeholder("you@example.com")` | `get_by_placeholder("you@example.com")` |
| `get_by_alt_text("Logo")` | `get_by_alt_text("Logo")` |
| `get_by_title("Close")` | `get_by_title("Close")` |
| `get_by_test_id("login")` | `get_by_test_id("login")` |

- `exact=False`, `exact=None` and `include_hidden=False` are left out, and so is `exact=True` where it changes nothing: for a regular expression, and for `get_by_role()` without `name=`. The options of `get_by_role()` come in the order `name`, `exact`, `level`, and a role passed as `role=`, or a text as `text=`, gives the same key.
- The text is kept as written, so a call that differs only in case or spacing is another key.
- In `overrides.json` and `journal.jsonl` the inner quotes are escaped by JSON, as in `"get_by_role(\"button\", name=\"Sign in\")"`. `mendwright report` and the messages show the key as it is.
- A string written as a call is how mendwright names a `heal.page.get_by_*()` call, so `heal.locator()`, `heal.locator_all()` and `heal.interrupt()` raise `ValueError` for one as a key, and they and `heal.step(when=...)` raise `InvalidSelector` for one as a selector. `heal.page.locator()` hands such a string to Playwright as written, as before.

`mendwright.get_by_key()` builds the key of a call, for `destructive_keys=`, `expect_values=`, seed files and `heal.present()`:

```python
pay = mendwright.get_by_key("get_by_role", "button", name="Pay")   # 'get_by_role("button", name="Pay")'
heal = mendwright.open(page, heal_get_by=True, destructive_keys={pay},
                       expect_values={mendwright.get_by_key("get_by_test_id", "total"): "money"})
```

A key matches one call only: `destructive_keys={pay}` marks `page.get_by_role("button", name="Pay")`, and a test that writes `name=re.compile("pay", re.I)` or `exact=True` makes another key, which isn't marked.
`destructive_keys=`, `expect_values=`, seed files and `heal.present()` read a key written as the same call in another spelling, such as `"get_by_role('button', name='Pay')"`, as the call's key, and two spellings of one call in `expect_values=` or a seed file raise an error. A string that isn't a call, such as `"page.get_by_role('button', name='Pay')"` from a log line, is a key of its own, and nothing a call does uses it. For a call written on an object like that in `destructive_keys=`, `expect_values=` or a seed file, mendwright logs a WARNING that gives the call's key.

Some calls are never keys. They go to Playwright as written, with one INFO line per healer and call:

```text
INFO mendwright: page.get_by_role('checkbox', name='Terms', checked=True) is not healed: checked= picks the element by its state, which a saved selector cannot keep. It goes to Playwright as written.
```

They are `get_by_role()` with `checked=`, `disabled=`, `expanded=`, `pressed=` or `selected=` (a heal is an XPath, which can't say "the checked one"), or with `include_hidden=True`; a call with an empty role, name or text; arguments that are not text, a regular expression or a whole number; a regular expression with flags other than `re.IGNORECASE`, `re.MULTILINE` and `re.DOTALL`; an argument this version of mendwright doesn't read, such as one a later Playwright adds; and a key longer than 1,000 characters.

### When a call heals

- Only when it matches nothing for `timeout_ms`.
- Only from its fingerprint, which mendwright records each time the call matches one element while the fingerprint tier is on (see [Heals without a model](#heals-without-a-model)). So turn the flag on while the suite passes, and keep the store between CI runs (see [Keep heals between runs](#keep-heals-between-runs)). A call with no fingerprint in the store, one that never matched or matched only with the tier off, is not healed: a call that never matched is more often a mistake in the call than a redesign.
- Never by a model, a gateway or a local model, even when one is configured, so no page leaves your machine for a call. A call names its element by role and words, and when it breaks those are what changed: a model given the old call could only guess a new name. The fingerprint holds what survives a redesign, such as ids, test ids, names, classes and ancestors, and the tier needs a strong sign of identity before it proposes anything.
- A call that matches several elements, one hidden element (unless its role is checkbox), or that the browser rejects, such as `level=` on a button, is not broken. Playwright runs it as written and raises its own error: a strict mode violation, its wait and timeout, or the selector error. Nothing is saved and no saved heal is used, also when the call starts to match several elements while mendwright waits.
- A saved heal answers only while the call matches nothing:

| The call matches | A saved heal matches one element | What happens |
|---|---|---|
| one visible element (or one of role checkbox) | anything | the call's element |
| nothing | yes | the heal's element, at once |
| nothing | no | it waits `timeout_ms`, then heals from the fingerprint or raises |
| several, one hidden, or the browser rejects it | anything | Playwright runs the call as written |

- When the call matches nothing, the registered interruptions are handled before the wait: a modal cookie banner can hide the rest of the page from the accessibility tree, and `get_by_role()` matches nothing until it is gone.

A heal logs one line, and its journal events are those of any [fingerprint heal](#heals-without-a-model):

```text
WARNING mendwright: healed 'get_by_role("button", name="Sign in")': get_by_role("button", name="Sign in") -> //button[@id='login'] (fingerprint, 0.0s)
```

When the call isn't healed, the line after `->` says why, for example:

```text
No matching element for: get_by_role("button", name="Sign in") (tried 1 candidate(s)).
  -> mendwright heals a get_by_* call only from the fingerprint it records each time the call matches one element, and 'get_by_role("button", name="Sign in")' has none in this store: it has not matched one element here while the fingerprint tier was on. A call that never matched is more often a mistake in the call than a redesign. Check its arguments against the page.
```

A fingerprint match that wasn't confident, or failed a check, ends with `mendwright asks no model to heal a get_by_* call: the call names its element by role and words, and those are what changed, so a model could only guess. Change the call by hand.`

### What a heal must pass

A heal of a call goes through the checks of any fingerprint heal: one match of the element's role in the captured HTML, not quarantined, and one visible match on the live page that still has the role. The role is the one mendwright read from the element the call matched, as for a selector without `role=`.
For a `get_by_role()` call, Playwright must also give the healed element the call's ARIA role, and the call's `level=` when it has one, by its own matcher on the live page. mendwright's role is mostly the tag, so a `<button role="tab">` that is no longer a tab, or an `<a>` that lost its `href`, would pass it. One that Playwright doesn't give the role is refused with `live element role mismatch: Playwright does not give it the ARIA role 'tab'`, and one it can't be asked about is refused too.

### What stays unhealed

Only a call made directly on `heal.page` heals. These go to Playwright as in 0.8.0, the real locator, or with `dismiss=True` a `GuardedLocator`, and nothing about them is saved:

- a call chained from a locator: `heal.page.get_by_role(...).get_by_text(...)`, `heal.page.locator("#form").get_by_role(...)`, or `.locator(...)` after a call. The inner call matches inside the outer one's elements, and a heal is checked on the whole page, so it could land outside the container, on the Save button of another dialog.
- `.first`, `.last` and `.nth()`: the call matches several elements by design, and a heal can't keep "the second Save".
- `.filter()`, `.and_()` and `.or_()`, a second matcher that one saved XPath would drop.
- `heal.page.frame_locator("#pay").get_by_role(...)`. Open mendwright on the frame instead: `mendwright.open(page.frame(name="pay"), heal_get_by=True).page.get_by_role(...)` heals. A call that matches nothing on the page but matches inside an iframe raises, saying so; mendwright doesn't follow it there.
- `heal.page.native.get_by_*`, the page passed to `open()`, `heal.page.main_frame.get_by_*` and `page.frames[i].get_by_*`
- a call that is never a key (above)

`expect()`, `wait_for()`, `count()`, `all()`, `is_hidden()` and `is_visible()` answer for the call as written, and so does a call passed as an argument, such as `drag_to(page.get_by_role(...))`.
`is_visible()` on a call never answers from a heal, unlike `heal.page.locator(selector).is_visible()`: it is how a test asks whether the page shows something with that role and those words, and a heal of another element must not answer that. So `if page.get_by_role("button", name="Next").is_visible():` skips the branch once the button is renamed, while `page.get_by_role("button", name="Next").click()` heals.
The reads that wait for the element, such as `inner_text()` and `is_checked()`, heal like the actions, and with `expect_values=` a read through a heal is checked (see [Checking what a read returns](#checking-what-a-read-returns)).

### Steps that may not be there

- An optional step written as `try: ... except TimeoutError:` still works, since a miss raises a `TimeoutError`. But a call that has a fingerprint can be healed before that, when the fingerprint finds a confident match for the missing element, and the step then acts on it. Write an optional step as `heal.locator(key, selector, optional=True)` (see [Optional steps](#optional-steps)), or through `heal.page.native`.
- A step of your own on a cookie banner that mendwright handles, such as `page.get_by_role("button", name="Accept all").click()`, doesn't click it. While the banner shows, the call finds its button, the step handles the banner first, and the click then waits out Playwright's action timeout for a button that is gone and raises Playwright's `TimeoutError`, as with the flag off. When the banner was handled earlier, the call matches nothing: the step waits `timeout_ms`, and the fingerprint tier, which recorded the banner's button when the call matched it, looks for it on a page without the banner, usually finds no confident match, and raises. Drop such a step, or write it as `heal.locator(key, selector)` with the interruption's key, which is skipped when the element isn't there.

### mendwright fix and calls

[`mendwright fix`](#mendwright-fix) never rewrites a call, with `--apply` or without. It lists it under "Left alone", with its healed selector and the Python lines that make a call with the same literal arguments:

```text
Left alone:
  get_by_role("button", name="Sign in"): it is a heal.page.get_by_role() call, and mendwright fix does not rewrite calls: from the code it cannot tell whether a call with these arguments runs on heal.page or inside another locator, where it may still find the element it was written for. Its healed selector, confirmed, is //button[@id='login']. Change the call's arguments by hand to the element's new role or words, or replace it with page.locator("//button[@id='login']"). Calls with these arguments: tests/test_login.py:12, tests/test_login.py:40.
```

From the code it can't tell a call on `heal.page`, which was healed, from one with the same arguments inside another locator, which still finds its own element. The arguments are often variables or constants that other calls share. And an XPath in place of the call swaps the role and words you chose for the most brittle form.

### What it costs

Each action or healed read of a call looks at the call first, in one round trip, two when it matches one element, then scrolls it into view: three more than Playwright's own call, as for `heal.page.locator(selector)`. The first lookup of a call on a healer also reads the element's role and fingerprint. Registered interruptions and `dismiss=True` add their usual checks before an action.
On a page of 2,000 elements, `heal.page.get_by_role("button", name="Continue").click()` took about 50 ms more than Playwright's own call in Chromium (the median of 50 clicks: about 80 ms against 34 ms), since Playwright's role engine reads the page for each query. Firefox and camoufox were close to that, patchright took about 70 ms more, and WebKit about 210 ms more.

### Limits

- A call whose element is inside a shadow root isn't healed once renamed: the fingerprint tier scores the captured HTML, and `page.content()` leaves shadow trees out.
- A call with no fingerprint in the store isn't healed (see above).
- `page.set_default_timeout()` isn't read: a missing call waits `timeout_ms`.
- `get_by_test_id()` uses the test id attribute set with `playwright.selectors.set_test_id_attribute()`, and the key doesn't say which one.
- `get_by_role()` matches part of the name unless `exact=True`, so after a redesign the call itself can find a different element ("Save" matches "Save draft") without mendwright noticing: it doesn't second-guess the code's own match. Pass `exact=True` for a name that another control could contain.

## Describing an element with intent=

`intent=` says what an element is for, in words:

```python
heal.locator("terms", "#accept-terms", role="checkbox",
             intent="the terms checkbox in the signup form").check()
```

It is saved with the key (the `intent` field in `overrides.json`), so later calls don't need to repeat it, and the model gets it as one line of the prompt when it heals the key.
It is redacted like the expected text: absolute URLs, long numbers and the row secrets you pass are replaced. Line breaks are folded into spaces.

With `role=` and no selector, and nothing saved for the key yet, a model looks for the element from the intent on the first call:

```python
heal.locator("newsletter", intent="the newsletter checkbox under the email field",
             role="checkbox").check()
```

What the model finds goes through the same checks as a heal. It is saved and used from then on, with no model call.
The log line is `found 'newsletter' from its intent: //input[@name='news'] (AnthropicProvider, 1.2s)`, and the journal events carry `"source": "intent"`.

- This needs a model provider. Without one, the error says `'newsletter' has no selector yet, and only a model can find it from intent=, but no model provider is configured.`
- Without `role=`, it raises `ValueError` before it looks at the page, because mendwright checks what the model finds against the role. In `mendwright.aio`, `locator()` raises it before anything is awaited.
- An `intent=` that is empty or not a string raises `TypeError`.
- The fingerprint tier is skipped for such a key, since nothing was recorded for it yet.

## Checking what a read returns

A heal passes the checks when the new element has the step's role and is the one visible match. For a read, that can still be the wrong element: after a redesign, a price selector can heal onto the rating next to the price, and a scraper saves 4.5 as the price. `expect_value=` says what a read must return:

```python
price = heal.locator("price", "#price", expect_value="money").inner_text()
```

When the element was found through a selector a heal saved, in this run or an earlier one, mendwright checks the value the read returns:

- A value that passes is returned. It confirms a heal made in this run, as `expect=` on an action does.
- A value that fails quarantines the heal, and the read raises `AttentionRequired` instead of returning it. The same text is logged as a WARNING:

  ```text
  mendwright: 'price' was healed to //span[@class='rating'], and its inner_text() there is '4.5', which is not money (expect_value='money'). The heal probably found another element, so mendwright quarantined it and did not return the value. The next lookup of 'price' heals it again, and cannot pick that element. If the value is right, change expect_value=.
  ```

  The quarantine blocks the element, not only the selector, so neither the fingerprint tier nor the model can pick the rating again (see [Actions and confirmation](#actions-and-confirmation)).

A read through any other selector, such as the one in your code, is never checked: `expect_value=` only ever refuses a heal. A price your own selector reads as "Call for price" comes back as it is.

The reads it checks are `inner_text()`, `text_content()`, `input_value()` and `get_attribute()` on Playwright, sync and async, and `text`, `get_attribute()` and `get_dom_attribute()` on Selenium. Other reads and the actions are as before. The elements of `heal.locator_all()` are never checked, since no list is found through a heal (see [Lists](#lists)).

| `expect_value=` | Passes for |
|---|---|
| `"money"` | an amount with a currency sign or code before or after it, and nothing else: `$1,299.00`, `1.299,00 €`, `129 kr`, `CHF 12.50`, `USD 1299`, `$12.00 USD`, `R1,299.00`, `₹1,29,999.00`, `129,-`, `-$12`. The signs are the Unicode currency signs and `US$`, `C$` and the like; the codes are the common ISO 4217 codes and abbreviations such as `kr`, `zł`, `Kč`, `Ft`, `lei`, `Rs`, `R`, `RM` and `CHF`. A number without one, such as `1,299.00`, is not money: use `"number"` |
| `"number"` | a number and nothing else, with an optional sign: `1,299.00`, `1.299,00`, `1 299,00`, `1'299.00`, `4.5`, `-3` |
| `"integer"` | a number with no decimal part written: `1,299`, `12`, but not `12.00` |
| `"percent"` | a number with `%` before or after it: `12%`, `12.5 %`, `%20` |
| `"date"` | a calendar date (31/02/2026 is not one): `2026-10-15`, with or without a time (`2026-10-15T10:00`, `2026-10-15T10:00:00+02:00`, `2026-10-15 10:00`), `15/10/2026`, `10/15/2026`, `15.10.2026`, `2026年10月15日`, or with the month's name in English, German, French, Spanish, Italian, Dutch or Portuguese, such as `Oct 15, 2026`, `15. Oktober 2026` or `15 de octubre de 2026`, also after a weekday or before a time |
| `"email"` | an email address, also as a `mailto:` link |
| `"url"` | an `http` or `https` URL with a host, one that starts with `//`, or a path that starts with `/`, `./` or `../`, as `get_attribute("href")` gives for a relative link |
| `re.compile(...)` | a value the pattern finds a match in, as `re.search()` does. Anchor it with `^` and `$` to match the whole value |
| a callable | a value it returns something truthy for. It gets the value as a `str`, and one that raises refuses it |

Numbers are read the way most locales write them. Commas, dots, spaces (non-breaking and narrow ones too) and apostrophes group the digits, in threes, or in India in twos before a last group of three (`1,29,999`). When a number has both `.` and `,`, the one that comes last is the decimal point, and a mark that comes more than once groups. A single `.` or `,` followed by exactly three digits, as in `1.299` or `1,299`, is read as a thousands separator, as in a price; `0,299`, `1234.567` and `1 299,500` have a decimal part. In an amount of a currency with three decimals (`KWD`, `BHD`, `OMR`, `JOD`, `TND`), that `.` is the decimal point, as those amounts are written: `KWD 1.250` is 1.250. That `,` could be either (`1,250 TND` is 1.250 in Tunisia and 1250 in English), so `parse_money()` gives `None` for it, though it passes `"money"`. The value is read in its compatibility form, so fullwidth digits and the digits of other scripts count, and spaces around it don't. A value of `None`, such as `get_attribute()` of an attribute the element doesn't have, passes no check, and a callable isn't called for it.

`mendwright.values.parse_money()` and `parse_number()` read the value you got the same way, and give the amount as a `Decimal`, or `None`:

```python
from mendwright.values import parse_money

parse_money("1.299,00 €")   # Decimal('1299.00')
parse_money("129 kr")       # Decimal('129')
parse_money("4.5")          # None: no currency
```

`heal.page` and `heal.driver` take no `expect_value=`, so give the checks for their keys to `mendwright.open()`. The key is the selector on `heal.page`, and `by=value` on `heal.driver`:

```python
heal = mendwright.open(page, expect_values={"#price": "money", "#delivery": "date"})
page = heal.page
page.inner_text("#price")       # checked when "#price" was found through a heal
```

The same dict covers keys of `heal.locator()`, and an `expect_value=` passed to it wins over it. Neither is saved, so pass them on each run. The elements of `heal.locator_all()` are never checked (see [Lists](#lists)).

In the journal, a refused read writes a `heal.canary` with `success` false (for a heal made in this run) and a `heal.quarantine`, each with a `reason` such as `expect_value: inner_text() is not money`. The value itself is never written.

## Lists

A list selector, such as `.product-card` for the cards of a product grid, matches many elements. `heal.locator_all()` gives an `ActionProxy` for each of them:

```python
cards = heal.locator_all("cards", ".product-card")    # an ActionProxy for each card
names = [card.native.locator("h3").inner_text() for card in cards]
```

mendwright never uses a list that your own selectors don't match. With `nonempty=True`, it looks for a list whose selector broke and proposes the new selector to you (see [below](#lists-that-always-have-items-nonemptytrue)). An emptied list can't be told from a list whose selector broke: when every item of a cart moves to Saved for later, the page can be what a redesign that renamed the cart's rows would give, but for a class name or a heading's text, and a heal there gives a loop that deletes the cart's rows the items saved for later. So such a lookup raises `AttentionRequired`, which names both possibilities, and a selector that broke in a redesign is fixed in your code.

`heal.page.locator(selector).all()` and `heal.driver.find_elements()` behave as in 0.7.0: they give what the real call gives, and never wait, heal or save anything. A list that matches nothing is `[]` at once, so a loop that waits for toasts or a spinner to go away, or a check that a cart is empty, works as before.

`heal.locator_all(key, selector=None, *, role=None, timeout_ms=None, poll_ms=200, optional=False, nonempty=False)` gives an `ActionProxy` for each element the list matches, in page order. `selector`, `role` (the items' role), `timeout_ms` and `poll_ms` mean what they mean for `heal.locator()`. `nonempty=` takes `True` or `False` (anything else raises `TypeError`), and `nonempty=True` with `optional=True` raises `ValueError`, before anything touches the page. In `mendwright.aio`, `await heal.locator_all(...)` gives `AsyncActionProxy`s, and those errors raise at the call.

How a list is looked up:

1. The key's selectors are tried in order, the one in your code first, then the others saved for the key, such as those of a seed file. The first that matches one element or more gives the list, as written. A list is looked for on the page, not inside an iframe. Heals saved under the key are skipped: the key can also be that of `heal.locator()`, of a `heal.page` shortcut (the selector) or of `heal.driver.find_element()` (`by=value`), and a heal of the first card is not a heal of the cards.
2. Each time one matches two or more elements, mendwright records a list fingerprint, in one round trip, once per Healer and selector: the [fingerprints](#what-a-fingerprint-holds) of three of the items (the first, the middle and the last), the tags inside each of them and of their children (their shape), how many items there were, and their nearest common container (its tag, id, test id, aria-label, classes and role). It tells mendwright that the list had items. Without `role=`, the items' role is saved from the first item. The first lookup of a key saves its selector, its first success, its page and the list fingerprint in one write of `overrides.json`. A list of one element is not recorded: nothing repeats in it.
3. When none matches and the key has a list fingerprint, mendwright waits up to `timeout_ms`, handles the registered [interruptions](#interruptions) the page shows, and raises `AttentionRequired`, logged as a WARNING too. Unless the call passes `nonempty=True`, it looks for no other list on the page and asks no model, even when one is configured, and saves nothing, so the next run raises again. A list that comes while it waits, such as cards that a slow API sends, is used as it is.
4. When none matches and the key has no list fingerprint, the list is empty at once, as `Locator.all()` and `find_elements()` give it. So is a list passed `optional=True`.

What a cart's lookup raises once every item of the cart moved to Saved for later, and once a redesign renamed its rows:

```text
No matching element for: cart (tried 1 candidate(s)).
  -> 'cart' matched 3 elements when its list fingerprint was recorded, and none of its selectors matches now. Either the list is empty at this point, or its selector broke: mendwright can't tell the two apart, since the items of an emptied list can show in another list, such as a cart's in Saved for later, so it never heals a list that matches nothing. If the selector broke, change it in your code.
```

For a list that may be empty at that point, such as a cart after an order, pass `optional=True`: the lookup gives `[]` at once when nothing matches, and never raises. It gives `[]` for a selector that broke too, so pass it only where an empty list is right. `heal.page.locator(selector).all()` and `heal.driver.find_elements()` never raise either.

With `fingerprint=False`, `MENDWRIGHT_FINGERPRINT=0` or `MENDWRIGHT_DISABLE=1`, no list fingerprint is recorded, and a list lookup that matches nothing is empty at once, as `Locator.all()` and `find_elements()` give it, even for a list recorded in an earlier run.

The elements' actions handle interruptions and popups as `heal.locator()`'s do, and confirm or quarantine nothing, and so does an element's `confirm()`: no list is found through a heal, and a heal of one element under the same key, such as of `page.inner_text(".card")` next to `heal.locator_all(".card", ".card")`, is not the element's. On Playwright, the first read of an element of `heal.locator_all()` handles interruptions too, once for the whole list (see [Interruptions](#interruptions)). The reads of the elements of `heal.page.locator(selector).all()` handle none. The reads of a list's elements are never checked against `mendwright.open(expect_values=...)`. With `dismiss=True`, `heal.page.locator(selector).all()` gives `GuardedLocator`s. List fingerprints are saved in `overrides.json` as `listFingerprint` (see [SECURITY.md](../SECURITY.md) for what they hold).

### Lists that always have items (nonempty=True)

`nonempty=True` says that a list always has items at that point of your flow, such as the rows of a cart your test just filled:

```python
heal.locator("add", "#add-to-cart").click()
rows = heal.locator_all("cart", ".cart-item", nonempty=True)
```

Don't pass it for a list that can be empty there. For that, pass `optional=True`, or nothing.

Add it while the list still works. mendwright records where the list sits and its heading the next time the list matches in a call with `nonempty=True`, and keeps recording them for that key after that, in calls with and without it. A list recorded before that, by mendwright 0.8.0 or by calls without `nonempty=True`, can't be looked for, and its lookup says so.

When none of the list's selectors matches, the lookup waits `timeout_ms` and handles interruptions, as any list lookup does, and also when mendwright has no list fingerprint for the key. If the list comes while it waits, it is used. Then mendwright looks for the list on the page: a group of 2 or more elements of the items' role that holds 2 of the 3 items it recorded, isn't under another heading or in another part of the page than the list was, and is visible on the live page. It never uses what it finds. It saves it in `overrides.json` as a proposed list heal, and raises `AttentionRequired`:

```text
No matching element for: cart (tried 1 candidate(s)).
  -> 'cart' matched 3 elements when its list fingerprint was recorded, under 'Shopping cart', in <section class="cart">, and none of its selectors matches now. nonempty=True says the list has items here, so mendwright looked for it and found 3 div elements, under 'Shopping cart', in <section class="cart">, that hold 3 of the 3 items it recorded: //div[contains(concat(' ', normalize-space(@class), ' '), ' cart-row ')] (as CSS: div.cart-row). mendwright never uses a list it finds, because the items of an emptied list can show in another list on the page. If this is the list, look at it with: mendwright lists. Accept it with: mendwright lists accept cart 8b5351a5. Then write it into your code with: mendwright fix --apply. If the list can be empty here, remove nonempty=True.
```

The commands get `--store "PATH"` when the store is not the default one. A WARNING says the same in one line: `mendwright: found a list for 'cart' (3 div elements under 'Shopping cart'): .cart-item -> //div[...] (0.2s). It is not used. Check it with: mendwright lists`.

What mendwright never does: it never gives you, or acts on, a list it found, in that run or a later one, and not after you accept it either. `heal.locator_all()` gives only what your own selectors match, those in your code or a seed file. A selector mendwright found works only once it is in your code.

Why: when every item of a cart moves to Saved for later, the page can be what a redesign that renamed the cart's rows would give, but for a class name or a heading's text. Two of the test pages of `tests/test_lists_emptied.py` are the same for an emptied list and a redesign but for one class name (`done` or `task`) and a heading. No rule on the page's markup is right on both, and a list taken from the wrong one gives a loop that deletes the cart's rows the items saved for later. So a person decides, looking at the page. mendwright helps with one more rule: it refuses a group that its selector picks by a word for a state that the list didn't have, in a class, id, name or text: `completed`, `complete`, `done`, `finished`, `archived`, `read`, `seen`, `saved`, `removed`, `deleted`, `cancelled`, `canceled`, `expired`, `inactive`, `closed`, `resolved`, `past`, `empty`, `unavailable` or `soldout`. A list that was about a state itself, such as a "Completed" list or items with class `done`, keeps its word. This says what the words mean, and is no proof: a site whose class names say nothing gets no help from it.

To check and accept a proposal, run `mendwright lists` (see [mendwright lists](#mendwright-lists)). It shows where the list was when it was recorded and where the group it found is, the first items of both, and what is not the same:

```text
cart  (div elements)  id 3f2a9c1d
  found    2026-11-02 10:14:03 UTC on /cart, from the list fingerprint (score 0.86, next best 0.31)
  was      .cart-item: 3 elements under 'Your bag', in <ul class="lines">
           first items: 'Blue mug$12.00Delete', 'Red mug$13.00Delete'
  now      //li[contains(concat(' ', normalize-space(@class), ' '), ' summary-line ')]
           3 elements under 'Order summary', in <ul class="summary">
           they hold 3 of the 3 items recorded
           first items: 'Blue mug$12.00Delete', 'Red mug$13.00Delete'
  check    The heading is not the same: 'Your bag' then, 'Order summary' now.
           The container is not the same: <ul class="lines"> then, <ul class="summary"> now.
           The items of an emptied list can show in another list. Look at the page before you accept.
  accept   mendwright lists accept cart 3f2a9c1d
  reject   mendwright lists reject cart 3f2a9c1d
```

Read the `check` lines, and look at the page. If it is the list, `mendwright lists accept cart 3f2a9c1d` accepts it, by the id you looked at: the id changes whenever what `mendwright lists` shows changes, so a proposal that changed since is not accepted. Then `mendwright fix --apply` writes the new selector into your code, as it writes a heal (see [mendwright fix](#mendwright-fix)). If it isn't, `mendwright lists reject cart` quarantines it, and mendwright never proposes it, or another selector for the same elements, again.

After you accept, the lookup still raises until the selector is in your code:

```text
No matching element for: cart (tried 1 candidate(s)).
  -> 'cart' matched 3 elements when its list fingerprint was recorded, and none of its selectors matches now. You accepted a list heal for it on 2026-11-02 10:20:00 UTC, //div[contains(concat(' ', normalize-space(@class), ' '), ' cart-row ')], which matches 3 elements now, but mendwright takes a list only from the selectors in your code. Write it there with: mendwright fix --apply. If the list can be empty here, remove nonempty=True.
```

When the key's selectors come from a seed file, `mendwright fix` can't write it, since it changes only your code: the lookup, `mendwright lists` and `mendwright report` say so, and say to put the new selector in the seed file in place of the old one. A seed file that holds the XPath of a list heal that waits takes it over, as your code does once `mendwright fix` wrote it.

When the selector a list heal replaced matches again, in any lookup of the key, with or without `nonempty=True`, a proposed list heal is no longer proposed (an INFO line), and an accepted one is quarantined, with a WARNING and a `heal.quarantine` event: the list was not broken, or the key is used on more than one page. A proposal names the selectors it replaces, so a key used with another selector on another page doesn't end it. `mendwright fix` writes an accepted list heal only over those selectors, and leaves the key alone, saying why, when your last run passed other selectors for it (another page's, or one you fixed by hand), or when one of its selectors matched after the list heal was found. So `mendwright fix` never writes another list's selector over a selector that works.

The selector it replaced matching one element is enough, since a list with one item is not empty: one element that keeps the old class elsewhere on a page, such as in a mini cart, quarantines a list heal you accepted too. mendwright can't tell it from the list, so it fails closed. From then on a lookup with `nonempty=True` refuses that group with `it is a list heal you rejected, or one mendwright quarantined`. If it is the list, change the selector in your code by hand; to have it proposed again instead, delete its row from `overrides.json` (see [Store and journal](#store-and-journal)).

What it refuses, each with its reason in the message:

| When | The message says |
|---|---|
| The list never matched 2 or more elements | `mendwright has never seen this list match 2 or more elements` |
| It was recorded before a call with `nonempty=True` | `its list fingerprint was recorded by mendwright 0.8.0 or before a call with nonempty=True matched the list` |
| The fingerprint tier is off | `mendwright looks for a list only while the fingerprint tier is on, and fingerprint=False turns it off.` (or what else turned it off) |
| The key is marked destructive | `is marked as a destructive control` |
| The page is not one the key was found on | `That looks like a change in the flow, not a renamed element` |
| Every group is another list | `each group of div elements on the page is another list` and why |
| The only groups look like placeholders | `look like placeholders`, so the list may still be loading |
| No group repeats | `the page has no group of 2 or more div elements that repeat the same structure` |
| No group is visible | `no group of div elements is visible on the live page` |
| The best group scores too low | `scores 0.52, below the 0.65 needed` |
| Two groups score alike | `two groups score alike` |
| The shapes differ | `are not all shaped like the recorded items` |
| Too few of the recorded items are in it | `holds only 1 of the 3 items it recorded`, or it shares values only with every item at once, or none |
| No selector without positions finds it | `no selector without positions matches the best group and nothing else` |
| A state word | `is picked by 'done-item', and 'done' names a state the recorded list didn't have` |
| The live checks | `failed mendwright's list checks` and the check, such as `it matches 1 element on the live page, and a list heal needs 2 or more` or `it is a list heal you rejected` |
| Dry run | `dry-run mode is on, so the list mendwright found for 'cart', ..., was checked but not saved.` |

No model or gateway is asked about a list, with or without `nonempty=True`, and the hourly limit isn't touched.

Limits:

- A list with one item now is not looked for: a group needs 2 or more.
- A list whose items change from run to run, such as live search results, can't be found: the group must hold 2 of the items that were recorded.
- A redesign that puts the list under a new heading while the old one stays on the page, or in a new wrapper next to a look-alike, is refused as another list. 12 of the 33 redesigns of `tests/test_lists_emptied.py` are refused, most of them so.
- An emptied list whose items show in another list whose class names say nothing (`notif`, `wish-line`) and with the same kind of heading is proposed: only you can tell. Each such case in the tests says that its heading is not the same.
- Two identical containers with no heading give you little to go on: the proposal then says `the 2nd of 2 alike`.
- Only the first 200 elements of a group are checked for their role and visibility on the live page.
- A wrong acceptance that `mendwright fix --apply` writes into your code before the old selector matched again is your code's selector from then on, as any wrong selector you write is.
- A mendwright 0.8.0 run on the same store drops a proposal's evidence, so it can't be accepted until a 0.9.0 run finds it again, and the next time 0.8.0 records the list it drops where the list sat.

In CI, a proposal lives in the cached store (see [Keep heals between runs](#keep-heals-between-runs)). The message in the job log has the new selector. Or download the store and run `mendwright lists --store PATH`, `mendwright lists accept KEY ID --store PATH` and `mendwright fix --apply --store PATH` on your machine.

In `overrides.json`, a proposal is a candidate with `"status": "proposed"`, `"list": true` and a `proposal` with what `mendwright lists` shows, and an accepted one has `"status": "accepted"` and an `acceptedAt`. Neither status is ever tried by a lookup. The list fingerprint gets a `label` (its heading) and a `place` (short hashes of where it sat). The journal gets `heal.held` for a proposal and `heal.accepted` for an acceptance (see [journal.jsonl](#journaljsonl)).

## Providers

### How one is picked

With no `provider=`, `auto_provider()` reads the environment and takes the first match:

| Order | Set this | Provider | Model from |
|---|---|---|---|
| 1 | `ANTHROPIC_API_KEY` | `AnthropicProvider` | `ANTHROPIC_MODEL` |
| 2 | `OPENAI_API_KEY` | `OpenAICompatibleProvider` (api.openai.com) | `OPENAI_MODEL` |
| 3 | `GEMINI_API_KEY` | `GeminiProvider` | `GEMINI_MODEL` |
| 4 | `MENDWRIGHT_GATEWAY` | `GatewayProvider` | the gateway |
| 5 | `MENDWRIGHT_LOCAL_MODEL` | `LocalModelProvider`, mendwright's own model in Ollama | `MENDWRIGHT_LOCAL_MODEL` |
| 6 | `OLLAMA_MODEL` or `OLLAMA_HOST` | Ollama, keyless | `OLLAMA_MODEL` |

`OLLAMA_HOST` defaults to `http://127.0.0.1:11434`, and `localhost` in it is read as `127.0.0.1`.
`MENDWRIGHT_PROVIDER` skips the order and forces one of the six. `MENDWRIGHT_PROVIDER=local` uses `mendwright-selector` when `MENDWRIGHT_LOCAL_MODEL` isn't set.
For a proxy or a compatible host, the Anthropic and OpenAI providers take a base URL from `base_url=`, or from `ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL`. `base_url=` wins.

mendwright has no default model. A key without its `*_MODEL` variable fails with an error that names the variable.
`mendwright doctor` shows the result:

```text
Model provider
  AnthropicProvider, model your-model-id
  chosen because ANTHROPIC_API_KEY is set
  provider variables set: ANTHROPIC_API_KEY, ANTHROPIC_MODEL (values not shown)
```

### Passing one

Bedrock, LiteLLM, Azure and other OpenAI-compatible hosts aren't detected from the environment. Build them and pass `provider=`:

```python
import mendwright
from mendwright import (AnthropicProvider, BedrockProvider, GatewayProvider, GeminiProvider,
                        LiteLLMProvider, LocalModelProvider, OpenAICompatibleProvider)

provider = AnthropicProvider(model="...")             # key from ANTHROPIC_API_KEY
provider = OpenAICompatibleProvider(model="...")      # OpenAI, key from OPENAI_API_KEY
provider = OpenAICompatibleProvider(model="...", base_url="https://api.groq.com/openai/v1", api_key="...")
provider = OpenAICompatibleProvider.for_ollama("...")  # http://127.0.0.1:11434/v1, no key
provider = OpenAICompatibleProvider.for_azure(endpoint, deployment, api_version, key)
provider = GeminiProvider(model="...")                # key from GEMINI_API_KEY
provider = BedrockProvider(model="...", region="us-east-1")  # AWS credentials from boto3
provider = LiteLLMProvider(model="...")               # any model string LiteLLM routes
provider = GatewayProvider("https://heal.example.workers.dev", "client-key")
provider = LocalModelProvider()                       # mendwright-selector in Ollama, see below

heal = mendwright.open(page, provider=provider)
```

`BedrockProvider` and `LiteLLMProvider` also read `BEDROCK_MODEL` and `AWS_REGION`, or `LITELLM_MODEL`, `LITELLM_API_BASE` and `LITELLM_API_KEY`.

Vendor SDKs are optional.
Anthropic, OpenAI-compatible hosts and Gemini fall back to a plain HTTPS call through the standard library when their SDK is missing.
Bedrock needs boto3: `pip install "mendwright[bedrock]"`.
LiteLLM needs the litellm package (`pip install "mendwright[litellm]"`), or an `api_base` that points at an OpenAI-compatible LiteLLM proxy.
The other extras are `[anthropic]`, `[openai]`, `[gemini]` and `[all]`. `[all]` installs every provider SDK and no browser library.

Any object with `propose(bundle)` and `report_outcome(key, xpath, success)` works as a provider. [CONTRIBUTING.md](../CONTRIBUTING.md) describes the contract.
On a page longer than 60,000 characters, a provider gets the bundle with the page fitted to the prompt (`mendwright.fit` says how), and while parts of the page are left out to fit, a proposal with a position in it is refused. A provider that reads the whole page itself sets `wants_full_html = True` to get it unchanged, as `LocalModelProvider` does.

### Local model

`LocalModelProvider` asks mendwright's own selector model, which runs in Ollama on your machine, so the page never leaves it.
It is Qwen2.5-1.5B-Instruct fine-tuned for one task, and it doesn't read the page or write XPath.
mendwright lists at most 40 elements of the step's role from the captured page, one short line each (tag, text, label, id, name, test id, aria-label, link path, the row or heading it sits under, its landmark, whether it is hidden).
The model gets that list and the step: the key, the role, the old selectors, the expected text and `intent=` if set, the fingerprint mendwright saved when the selector last matched, and the page path now and where the element was last found.
It answers with the number of the element the step meant, or says that none fits and why.
mendwright then builds the XPath for that element itself, from its id, test id, name, aria-label, label, text or the row or heading around it, never from its position, and checks it like any other heal.

`mendwright model install` downloads the model file (about 1 GB, once) from the GitHub release this version of mendwright was tested with, checks its SHA-256, writes an Ollama Modelfile next to it and runs `ollama create mendwright-selector`. It needs [Ollama](https://ollama.com/download). `--dir` picks where the file is kept (default: a mendwright folder in your user cache), `--name` the Ollama name, `--skip-create` only downloads, and `--force` downloads again. To build the model yourself, [training/README.md](../training/README.md) has the recipe and its accuracy. Then:

```bash
mendwright model install
export MENDWRIGHT_LOCAL_MODEL=mendwright-selector   # or the --name you gave it
mendwright doctor                                    # LocalModelProvider (mendwright selector model in Ollama)
```

In code, `LocalModelProvider(model="mendwright-selector", base_url="http://127.0.0.1:11434")`. Both arguments are optional: the model falls back to `MENDWRIGHT_LOCAL_MODEL` and then `mendwright-selector`, and the host to `OLLAMA_HOST` and then `http://127.0.0.1:11434`. `localhost` is read as `127.0.0.1`, because on Windows a request to `localhost` tries IPv6 first and waits about 2 seconds before Ollama answers on IPv4.

Three things differ from the other providers:

- It is the only provider that gets the key's fingerprint. A model on your own machine needs it to compare, and the fingerprint never goes to a hosted model or a gateway.
- When the model says no element fits, the heal is declined with `the local model says no element on this page fits:` and the reason (the element isn't on the page, several fit equally, or the step seems to have moved to another page). When mendwright's checks refuse the element it picked because it isn't visible or isn't unique on the live page, the model is asked once more without that element. After any other refusal it doesn't guess again.
- It declines a pick the model isn't sure of. The provider asks Ollama for the probabilities of the reply's tokens, and a pick under 0.8 is declined with `the local model was not sure which element the step meant`, the element it picked and the confidence. On held-out test pages this cut wrong picks from 1.2% to 0.14% of broken selectors, for one point fewer correct ones. `min_confidence=` or `MENDWRIGHT_LOCAL_MIN_CONFIDENCE` changes the threshold, and 0 turns it off. An Ollama that returns no probabilities skips the check.

### Direct or gateway

A direct provider runs in your process with your API key.
That suits your own machine, your CI and servers you run.

A tool you hand to other people is different: every copy would carry your key, and anyone with a copy can read it.
[examples/heal_gateway.mjs](../examples/heal_gateway.mjs) is a Cloudflare Worker that keeps the key on the server.
The client sends it the same redacted page bundle a direct provider gets, with the shared client key (the worker's `HEAL_KEY` secret) in the `X-Heal-Key` header.
The worker allows 3 `/heal` requests per selector key and 60 requests in all per hour, keeps an audit trail in KV, and records outcomes from `/heal/outcome`.
Outcome reports count toward the 60, and a re-prompt after a rejected proposal is a second `/heal` request.
Its reply is treated like any model reply, so every local check still runs on the client.

Setup, from [examples/wrangler.toml](../examples/wrangler.toml):

```bash
wrangler kv namespace create HEAL_KV     # put the id into wrangler.toml
wrangler secret put HEAL_KEY             # the key your clients send
wrangler secret put ANTHROPIC_API_KEY    # or OPENAI_API_KEY with PROVIDER = "openai"
# set MODEL in the [vars] section of wrangler.toml; the worker has no default model
wrangler deploy
```

Clients then set `MENDWRIGHT_GATEWAY` to the worker URL and `MENDWRIGHT_KEY` to the `HEAL_KEY` value.

## pytest plugin

mendwright registers a pytest plugin under the entry point name `mendwright`.
pytest loads it in every run where mendwright is installed. `-p no:mendwright` turns it off.
A run that uses none of it prints nothing extra and writes no files.

### The heal fixture

`heal` is function-scoped and returns `mendwright.open(page, store_path=...)` for the test.
`page` is the fixture named by `--mendwright-page-fixture`, then the `mendwright_page_fixture` ini key, then `page` from pytest-playwright.
For Selenium, point it at your driver fixture:

```ini
[pytest]
mendwright_page_fixture = driver
```

In `pyproject.toml` the same key goes under `[tool.pytest.ini_options]`.

The fixture's store is the first of these that is set:

1. `--mendwright-store PATH`, relative to where pytest was started
2. the `mendwright_store` ini key, relative to the ini file
3. `MENDWRIGHT_STORE`, relative to where pytest was started
4. `.mendwright/overrides.json` in the rootdir

A Healer that a test opens itself with `mendwright.open()` uses its own `store_path`, or `MENDWRIGHT_STORE`, or `./.mendwright/` in the current directory.

The model comes from a fixture named `mendwright_provider`. By default it returns `None`, which means "pick one from the environment" as `mendwright.open()` does. Override it in a `conftest.py` to choose a provider for the `heal` fixture:

```python
import mendwright
import pytest

@pytest.fixture
def mendwright_provider():
    return mendwright.OpenAICompatibleProvider(model="...", base_url="http://localhost:1234/v1")
```

### Options

| Option | Meaning |
|---|---|
| `--mendwright-page-fixture NAME` | The fixture the `heal` fixture wraps |
| `--mendwright-store PATH` | The store for the `heal` fixture |
| `--mendwright-dry-run` | Sets `MENDWRIGHT_DRY_RUN=1` for the run |
| `--mendwright-disable` | Sets `MENDWRIGHT_DISABLE=1` for the run |
| `--mendwright-strict` | Fails every test that needed a heal |
| `--mendwright-heal-get-by` | Sets `MENDWRIGHT_HEAL_GET_BY=1` for the run, so `heal.page.get_by_*` calls heal (see [get_by locators](#get_by-locators)) |

The dry-run flag also reaches Healers that tests open themselves, unless that code passes `dry_run=`, and so does the get_by flag, unless it passes `heal_get_by=`. The disable flag reaches all of them, and no argument overrides it.

### Confirmation

When a test passes, every heal made during it is confirmed. That covers the `heal` fixture and any Healer the test opened with `mendwright.open()` or `mendwright.aio.open()`.
When a test fails, its heals stay saved and unconfirmed, and the failure report gets a section like this:

```text
--------------------------------- mendwright ----------------------------------
1 selector was healed during this test:
  newsletter: //input[@id='news'] -> //input[@id='newsletter-v2']  (not confirmed: the test did not pass)
A heal from a failed test is kept but not confirmed. If the failure is about that element, check the new selector before the next run.
```

With `GatewayProvider`, each confirmation is an HTTP call to the gateway.

At the end of a run that healed something, the plugin prints a summary:

```text
================================= mendwright ==================================
mendwright: 3 selectors healed in 3 tests (2 confirmed by passing tests, 1 not confirmed)
  test_checkout.py::test_accept_terms  accept_terms: //input[@id='terms'] -> //input[@id='terms-v2']
  test_checkout.py::test_search  search: //input[@id='search-box'] -> //input[@name='q']
  test_checkout.py::test_newsletter_then_an_unrelated_failure  newsletter: //input[@id='news'] -> //input[@id='newsletter-v2']  (not confirmed: the test did not pass)
```

It lists the first 10 heals and says how many more there are; `-v` lists them all, and `-q` prints only the count line.

A test that ended in a list heal proposed for `heal.locator_all(..., nonempty=True)` failed with the proposal's message, and the summary names its key, up to 10 keys, also with `-q` and under pytest-xdist:

```text
mendwright: 1 list heal waits for you to check it (run mendwright lists): cart
```

A passing test never accepts a list heal: only `mendwright lists accept` does (see [Lists that always have items](#lists-that-always-have-items-nonemptytrue)). With `--mendwright-dry-run`, nothing is proposed.

### Strict mode

`--mendwright-strict` fails a test that passed only because a selector was healed. The heal is still saved and confirmed:

```text
______________________________ test_accept_terms ______________________________
mendwright --mendwright-strict: this test passed only because 1 selector was healed. The heal is saved, but the selector in your code no longer matches the page:
  accept_terms: //input[@id='terms'] -> //input[@id='terms-v2']
Update the selector in your code to the new one, or run without --mendwright-strict to let heals pass.
```

For a `get_by_*` call, the message ends with `For a get_by_* call, run mendwright fix to see the healed selector and where the call is; it does not change the call itself.`

Strict mode only fires on the run that makes the heal.
It fails tests that needed a heal, and nothing else: a handled interruption (`flow.interrupt`), a dismissed popup (`flow.dismiss`) or steps that came in another order (`flow.step`, see [Steps in any order](#steps-in-any-order)) don't fail a test.
On the next run with the same store, the saved heal resolves as a fallback, no heal happens, and the test passes.
For a strict CI job, give each run a fresh store (see [CI](#ci)).
Strict mode also covers reruns from pytest-rerunfailures: a rerun that passes only because of a heal fails too.

### pytest-xdist and async tests

Store writes take a lock file and merge with what other processes wrote, so `pytest -n auto` can share one store.
The controller prints the summary once.
Under xdist the one-line strict reason is missing from the short summary, but the full failure text is there.

The `heal` fixture is sync. In an async test, open the healer yourself with `heal = await mendwright.aio.open(page)`. The plugin still confirms its heals when the test passes.

## Async Playwright

`mendwright.aio.open(page)` takes an async Playwright `Page` or `Frame`, including async patchright and camoufox objects.
It takes the same keyword arguments and environment variables as `mendwright.open()`. Await it inside the event loop that runs the page.

```python
heal = await mendwright.aio.open(page)

await heal.locator("accept_terms", "//input[@id='terms']", role="checkbox").check()

box = await heal.locator("newsletter", "#newsletter")   # resolve now, act later
await box.check(expect=box.native.is_checked)            # expect= may be sync or async
print(await heal.present("newsletter"))
```

- `heal.locator()` is a plain call that returns at once. Argument errors still raise there.
- The element is resolved, and healed if needed, when you await an action, a method such as `inner_text()`, or the proxy itself.
- A `heal.locator(...)` that is never awaited does nothing.
- `proxy.native` raises `RuntimeError` until the proxy has been awaited.
- `pending()`, `confirm_all()` and `heals` are sync, as on the sync Healer.
- `heal.interrupt()`, `cookies=`, `dismiss=True`, `optional=True` and the page guard work as on the sync Healer. `interrupt()` is a plain call; the action is awaited when the interruption is handled, and so is a `run=` function, an async function here (or a plain one that returns an awaitable), with no time limit, and it is cancelled with the step it runs for. Playwright's overlay handler for it is registered by a task on the event loop, in place a moment later. `interrupt(key, None)` removes it the same way, and the changes to one page's handlers run one at a time, in the order of the calls. `proxy.present` raises `RuntimeError` until the proxy has been awaited.
- Actions awaited together, for example with `asyncio.gather()`, look for interruptions one at a time, so two of them never click the same banner.
- `heal.step()` is a plain call, and `await heal.run_steps()` runs the steps (see [Steps in any order](#steps-in-any-order)). A step's function is an async function, which is awaited, or a plain one that returns an awaitable, such as `lambda page: page.click("#place")`. `heal.page`'s calls are coroutines here, so a plain function that makes them without `await` would do nothing on the page: `run_steps()` raises `TypeError` for a function that returns anything else, and the journal records the step as failed.

A heal runs the same sync coordinator in a worker thread through `asyncio.to_thread`, so the model call doesn't block the event loop.
Every page call the heal makes is handed back to the event loop and waited for, up to `mendwright.aio.SyncBridge.timeout` (30 seconds).
Calls on one `AsyncHealer` take turns, except those of a `run=` function, which go ahead while the step that called it waits. `asyncio.gather()` over several of its locators is safe, but they resolve one after another.
Use one `AsyncHealer` per event loop.

## Selenium

`mendwright.open(driver)` takes a Selenium WebDriver.
Selenium runs XPath and CSS. A Playwright selector such as `text=Save` raises `InvalidSelector` before any browser call.

Proxy actions use Playwright names and are mapped to WebElement calls:

| Action | On Selenium |
|---|---|
| `fill(value)` | `clear()`, then `send_keys(value)` |
| `check()`, `uncheck()`, `set_checked(bool)` | `click()` only when the state differs. Raises `RuntimeError` if the click did not change it. |
| `select_option(value)`, `label=`, `index=` | selenium's `Select`, by value and then by visible text |
| `type(text)`, `press_sequentially(text)` | `send_keys(text)` |
| `press("Enter")`, `press("Shift+Tab")`, `press("Control+a")` | `send_keys` with selenium `Keys` |
| `hover()`, `dblclick()` | `ActionChains` |
| `click()`, `clear()` | the WebElement methods |
| `send_keys()`, `submit()` | passed to the WebElement as they are |

Playwright's `timeout=`, `force=`, `no_wait_after=` and `delay=` are accepted and ignored, so step code can pass them on both libraries.
Any other option raises `TypeError`.
`tap`, `focus`, `set_input_files` and `drag_to` have no WebElement method and raise `AttributeError`.

mendwright's probes (count, visibility, the lookup before an action) set the implicit wait to 0 and restore it afterwards.
So `driver.implicitly_wait(10)` doesn't turn each missed candidate into a 10 second stall. Actions keep your implicit wait.

For a stored candidate that has a `frameXPath`, `proxy.native` is a `FramedElement`.
It switches into the frame for each call and back to the top document afterwards.
A raw `WebElement` found inside a frame stops working once the driver leaves it, so `driver.execute_script()` and `ActionChains` need `in_frame()` to get a usable element:

```python
from selenium.webdriver import ActionChains

box = heal.locator("frame_terms")      # stored with a frameXPath
box.native.in_frame(lambda el: driver.execute_script("arguments[0].scrollIntoView()", el))
box.native.in_frame(lambda el: ActionChains(driver).move_to_element(el).perform())
```

`in_frame(fn)` switches into the frame, finds the element again, calls `fn(element)`, and switches back. `Select(proxy.native)` doesn't work on it; use `proxy.select_option(...)`.

### Other browser libraries

Any object with `count`, `is_visible` and `handle` methods is used as a driver as is.
To have `mendwright.open()` recognise your library's page object, register a detector and a factory:

```python
from mendwright import register_driver

register_driver(detect=lambda obj: hasattr(obj, "my_find"), make=MyDriver)
```

[CONTRIBUTING.md](../CONTRIBUTING.md) lists the methods a driver needs.

## Iframes

mendwright opened on the page works with iframes one level down, on Playwright (sync and async) and Selenium.

- **A selector that matches only inside an iframe.** When a key's selector matches nothing on the page but matches exactly one element inside one iframe, mendwright looks for it there from now on, with no model call. It saves the selector with its `xpath=` or `css=` prefix written out and the iframe's selector as `frameXPath`, for example `iframe[id="consent-frame"]`, built from the iframe's id, name or title, or from its `src` up to the query (the query itself is never saved). An iframe with none of these isn't followed: a bare `iframe` could match another iframe on a later page. It goes through the live check like a heal, logs `found 'terms' inside the iframe ...`, and counts as a heal with source `frame`, so strict mode fails on it and the canary checks it.
- **A key last found inside an iframe.** When its selector breaks, the heal runs in that iframe: the fingerprint tier and the model get the iframe's HTML, the checks run in the iframe, and the new selector is saved with the same `frameXPath` and its `xpath=` prefix written out, so it never shares a row in the store with the same XPath on the page. A fingerprint read while the key matched inside the iframe is used there.

mendwright does not follow a key into an iframe, and says why in the `AttentionRequired`, in dry run, under `MENDWRIGHT_DISABLE=1`, while a `guard=` blocks heals, when the key is marked destructive, when the same lookup was quarantined before, when its selector matches in more than one iframe or more than one element there, when the iframe is inside another iframe, when nothing tells the iframe apart from the others on the page, for a Playwright engine selector (`text=`, `role=`), and on a page the key was never found on (the [page guard](#steps-that-moved-to-another-page)).
On Selenium, mendwright looks only one level down from the page, so a match deeper than that isn't seen at all, and the key heals on the page as before. For those, and for anything deeper than one level, open mendwright on the frame itself.

Playwright:

```python
frame = page.frame(name="consent")      # or any Frame from page.frames
consent = mendwright.open(frame)
consent.locator("frame_terms", "//input[@id='inner-terms']", role="checkbox").check()
```

Selenium:

```python
from selenium.webdriver.common.by import By

driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, "#consent-frame"))
consent = mendwright.open(driver)
consent.locator("frame_terms", "//input[@id='inner-terms']", role="checkbox").check()
driver.switch_to.default_content()
```

On Selenium, switch into the frame each time before you use that Healer.

The real-browser tests `test_a_selector_that_matches_only_inside_an_iframe_is_followed_there`, `test_a_key_last_found_in_an_iframe_heals_inside_it` and `test_the_iframe_workaround` cover these on every engine, including that the captured HTML is the iframe's.

## Layout changes

A heal happens when a stored selector stops matching on the page mendwright was opened on. That makes it a fit for layout changes inside one page: renamed or generated ids and classes, reworded or translated labels, new wrapper elements, elements moved to another region, and form fields in a new order. The benchmark has cases for each of these ([docs/benchmark.md](benchmark.md)).

Positional selectors are a blind spot. `(//form//input)[1]` keeps matching after the email and password fields swap places, so it now points at the password field. No selector broke, so mendwright never runs. Anchor selectors on something that travels with the element: an id, a name, a label or its text.

## Flow changes

A flow change is a change in the steps, not in the page: a cookie banner that opens before signup instead of after it, a step that moved to the next page, a new step in the middle. A heal can't fix these, and a model that is asked anyway may find a similar element that passes every check, such as a Help button where a Continue button used to be.

What mendwright does with each kind:

| Change | What mendwright does |
|---|---|
| A step that can show up anywhere: a cookie banner, a newsletter modal, an "are you still there?" dialog, a sign-in after the session expired | Handles it when it is in the way, if you registered it with `heal.interrupt()` or `cookies=`, with a function of yours for one that needs a form (`run=`) |
| A popup nobody registered: a newsletter or discount modal, a survey, a chat invite | Dismisses it when it covers the element of an action, if you pass `dismiss=True`: Escape, then its close button, then a button such as No thanks |
| A step that may not be there | Skips it, if you pass `optional=True` |
| A step that moved to another page | Declines to heal the key on a page where it was never found, before any model call |
| A step that moved away while the address stayed the same | Leaves it to the model, which is told it may answer that no element fits. The error then gives the model's reason. |
| Steps in another order, put on one page, or left out, such as a checkout that now asks for the card before the address | Runs whichever step the site shows, each once, if you register the steps with `heal.step()` and run them with `heal.run_steps()`. A step left out is named in a WARNING when a step has `last=True`; with none, `run_steps()` waits for it and stops with an error |

With `heal.run_steps()`, steps in a new order or on one page need no change to your script, and nor does a step the site leaves out, when the step that ends the flow has `last=True`. These still need one:

- a new required step, such as a field you must fill before you can continue: `run_steps()` stops on its page with an error that names the page
- an action that moved from one step to another, since each step's function does only its own actions
- a step whose landmark changed, since landmarks are never healed
- a flow where your script picks the way, such as delivery or pickup

### Interruptions

https://github.com/user-attachments/assets/4c88aaef-a6af-49f6-ac33-d3a6f8be4572

```python
heal = mendwright.open(page)
heal.interrupt("cookies", "#cookie-accept")
heal.interrupt("newsletter", "//div[@id='newsletter']//button[@aria-label='Close']")
heal.interrupt("still_there", "#idle-dialog", action="press", args=("Escape",))

heal.locator("email", "#email").fill("ada@example.com")
```

`heal.interrupt(key, selector, *, action=None, args=(), role=None, run=None)` registers a step that can show up at any point.
`selector` finds the element to act on: one selector, or a list tried in order. `action` and `args` say what to do with it, with the same verbs as a proxy action, and a click when `action` is left out. With `role=`, mendwright acts only on an element of that role (on drivers that can read elements). For one that needs a whole form, such as a sign-in after the session expired, pass your own function as `run=` instead (see [Interruptions that need a form](#interruptions-that-need-a-form)).
Use a selector that matches one element. An XPath that matches several is narrowed to the first visible match of its first 5, but a CSS or Playwright selector that matches several, such as `.cookie-accept` matching a hidden mobile copy and the visible button, is skipped. When none of the interruption's selectors finds it, mendwright logs one WARNING per key and page that says how many elements such a selector matches and, when exactly one of them is visible, names selectors that match only that one:

```text
WARNING mendwright: interruption 'cookies' was skipped: its selector '.cookie-accept' matches 2 elements, and mendwright acts on a CSS or Playwright selector only when it matches one (an XPath is narrowed to its first visible match). Use a selector that matches only the element to act on, such as '#accept-all' or 'role=button[name="Accept all"]', which each match one visible element on this page.
```

The selectors it names are built from the visible match: CSS from its id, test id or other attributes, and, for a text of at most 60 characters, `role=button[name="..."]` (`role=link[name="..."]` for a link) and `text="..."` on Playwright, and an XPath of its tag and whole text. It names at most two, and only those that match one visible element on the page now. When more than one of the matches is visible, such as a chat widget's Close button and the banner's own, it names none, since one built from the first could be the wrong element, and says so: `More than one of them is visible, so mendwright cannot tell which one is meant.` When none fits, the WARNING says what to look for instead, and that none of the matches is visible, when that is so. An XPath that matches more than 5 elements, none of the first 5 visible, gets the same WARNING. A list whose next selector finds the interruption gets none. It costs a few round trips, once per key and page. The `cookies=` preset's own selectors are never named.

mendwright looks for registered interruptions only when a step needs help:

- when a `locator()` misses, before any heal and before any model call
- before an action, when the page shows one of them (a quick check for each registered interruption, only when you registered any)
- on Playwright, before the first read of a `heal.locator()` proxy, such as `text_content()`, `inner_text()` or `get_attribute()`, or of an element of `heal.locator_all()`, once for the whole list, unless an action of the proxy or of one of the list's elements came first. Reading the proxy again, or the next element, doesn't look again, so a banner that opens after that first read is left to the next action, or to the first read after the next lookup. Each read of `heal.page` and `heal.page.locator(selector)` that goes through a proxy is a lookup of its own, so it looks each time: with `cookies=`, on a page of 2,000 elements in Chromium, that added 15 to 45 ms to each such read. Their `is_visible()` answers at once and doesn't look, and neither does a read of a selector that goes to the real page as written (see [Playwright](#playwright)).
- when an action fails because something covered the element: Playwright's "intercepts pointer events" or "element is not visible", Selenium's `ElementClickInterceptedException` or `ElementNotInteractableException`

On Selenium, the elements `heal.driver` returns look for registered interruptions in the same way before `click()`, `send_keys()`, `clear()` and `submit()`, whatever `By` found them (see [No changes per step](#no-changes-per-step)).

- On Selenium, the check before an action, of `heal.locator()` and of `heal.driver`'s elements, handles an interruption only when something covers the element where a click on it lands, or when that can't be told (for an element outside the viewport, or inside an iframe). So a step of your own on the interruption itself goes ahead and does not find it gone: the banner's Reject or Manage preferences button, or the email field of a newsletter form. If the action fails because something covered the element after all, the interruptions are handled and it is tried once more, as above.
- `ActionChains` actions and the elements that `execute_script()` or `switch_to.active_element` return are Selenium's own, so nothing is checked before their actions.
- After the script calls `switch_to.frame()` itself, mendwright looks for interruptions in that frame only, and a banner on the page around the frame is out of reach. When a click there is intercepted, the WARNING says so. Find the element from the page without switching instead: `heal.driver` follows a selector into an iframe one level down (see [Iframes](#iframes)).

On a Playwright `Page`, from Playwright 1.42, `heal.interrupt()` and `cookies=` also register each interruption with `page.add_locator_handler()`. Playwright then checks for it before each locator or selector action on the page (`click`, `fill`, `check`, `hover` and so on), and again each time an action that waits (for a disabled button, say) tries once more, and hands over to mendwright when the page shows it. So it is handled before the actions mendwright does not route as well: `get_by_role()` and the other `get_by_*` locators, chained locators, `.first` and `.nth()`, and the page's own selector methods, on `heal.page` or on the page itself. A banner that opens while an action waits is handled at once, instead of after the action's timeout. With `heal_get_by=True`, mendwright routes the `get_by_*` calls of `heal.page` itself: their actions handle the interruptions first and log them before the call's key, as `heal.locator()`'s do (see [get_by locators](#get_by-locators)).

- Playwright runs no handler before `page.mouse` and `page.keyboard` actions, `dispatch_event()`, or an action with `force=True`, so those get no such help.
- Such an action has no key, so the WARNING line and the journal event say `before '<page action>'`. One that happens during the action of a step mendwright routes, such as a banner that opens while its click waits, is logged before that step and counts toward its 3. Playwright's handler stays out of mendwright's own lookup, whose scroll runs the check too, as it stays out of mendwright's own clicks on a banner: the step handles what the page shows itself, before its action or read.
- An interruption that is still on the page after mendwright handled it 3 times in a row this way is left alone, with one WARNING, until mendwright sees it gone (a step that mendwright routes looks for it) or the page navigates or loads: `goto()`, a link, `set_content()`, or a history change in a single-page app.
- A page keeps one handler per key, however many healers you open on it: one that registers the same selectors joins the handler there, which acts for the newest of them that is still alive. Registering a key with other selectors replaces it. From Playwright 1.44 the old one is removed from the page, unless the handler of another key watches the same selectors, because Playwright would remove both. Before 1.44, and in that case, it stays and does nothing.
- `heal.interrupt(key, None)` removes an interruption from the healer, `cookie_banner` included, and its handler from the page (Playwright 1.44 and later) once no other healer of the page has registered it. A healer you stop using is not freed at once, because it refers to itself (through `heal.page`, for one): Python frees it when its garbage collector next runs, and until then its handler still acts. When the page outlives the healer, such as a page shared between tests, call `heal.interrupt(key, None)` for each key once you are done with it.
- Playwright runs the check at least twice per action, as it finds the element and as it acts, and again while it waits or retries. So each registered interruption costs at least two queries of the page per action. With `cookies=`, on a page of 2,000 elements in Chromium, that added 15 to 50 ms to each action.
- A selector the browser cannot parse would fail every action on the page in that check, with an error that names the interruption's selector. `heal.interrupt()` checks XPath without a browser but leaves CSS to the browser (`'button:contains("Close")'` passes it), so mendwright tries each CSS or Playwright selector once on the page when it registers the handler. One the browser rejects is left out, with a WARNING that names it.
- Playwright also runs the check before an auto-waiting assertion, `wait_for_selector()`, `Locator.wait_for()`, an element screenshot and `aria_snapshot()`, on the page itself too. So `expect(page.locator("#cookie-banner")).to_be_visible()` and `page.wait_for_selector("#cookie-banner")` fail once the banner is registered: mendwright closes it first, and the wait runs out. A step of your own that clicks the banner through a locator mendwright does not route, such as `page.get_by_role("button", name="Accept all").click()`, now finds it gone and waits out its timeout. Drop such a step, or write it as `heal.locator(key, selector)` with the interruption's key, which is skipped when the element isn't there. With `heal_get_by=True`, a `heal.page.get_by_*` step on the banner does the same while the banner shows: it handles the banner first, then waits out Playwright's timeout. When the banner was handled earlier, it waits `timeout_ms` and raises instead, since the fingerprint usually finds no confident match for an element that is gone (see [Steps that may not be there](#steps-that-may-not-be-there)): the same advice holds. Likewise, a read through `heal.locator()` of an element inside the banner under another key, such as its text, closes the banner first and then waits out its timeout: read it from the real page (`heal.page.native`) instead.
- A `Frame` has no `add_locator_handler()`, so a healer opened on one, and Playwright before 1.42, keep the checks above only.

When one is there and visible, mendwright performs its action (or calls its `run=` function), waits up to 2 seconds (or the step's `timeout_ms`, when that is shorter) for it to go away, logs one WARNING line, writes a `flow.interrupt` journal event, and tries the step again.
A failed action is tried again once, and only after an interception error and a handled interruption, so an action that may already have taken effect is never repeated. An error whose Playwright call log says the action was done (`click action done`), such as a click that went through and then timed out waiting for the next page, is never tried again, even when an earlier attempt in the same log was intercepted.
At most 3 interruptions are handled for one step (one `locator()` call, or one action). If one is still there after that, mendwright raises `AttentionRequired` and tries no heal. That stops an interruption that comes back each time it is closed, for example when its action doesn't close it.

An interruption is never healed by a model. If your script also has a step of its own for it, keep it: `heal.locator("cookies", "#cookie-accept").click()` is skipped when the element isn't there, either because the interruption was handled earlier in the run (no wait) or because it didn't appear this time (after `timeout_ms`). A skipped step logs one INFO line, its proxy has `.present` False, and its actions do nothing. When the banner does come at its old place in the flow, the step clicks it as before.

A skipped step can't tell a banner that didn't appear from one whose selector no longer matches. In the second case the banner stays, and a later action fails because it covers the element. mendwright then logs one WARNING that names the interruptions it looked for (yours, and the `cookies=` preset when it is on), because none of them matched a visible element, and raises the browser's error. The same WARNING comes when the element couldn't be reached for another reason, such as being hidden, while interruptions are registered.

An interruption that hides the element instead of covering it, such as a consent wall that replaces the page until you answer, is found when the step misses, so the step first waits its `timeout_ms`.

### Interruptions that need a form

```python
def relogin(page):
    page.fill("#relogin-user", "ada@example.com")
    page.fill("#relogin-password", os.environ["SHOP_PASSWORD"])
    page.click("#relogin-submit")

heal.interrupt("session_expired", "#session-expired", run=relogin)
```

Some interruptions take more than one click: a session-expired popup that asks you to sign in again, a form for your address or postcode, a confirmation with two fields. Pass the function that answers it as `run=`, in place of `action=` and `args=`. mendwright calls it at the same moments as any interruption, when `selector` matches one visible element: when a lookup misses, before an action, after an action was intercepted, and from Playwright's overlay handler. It gets `heal.page` on Playwright and `heal.driver` on Selenium, so its own selectors heal as usual. The selector only shows that the interruption is there, so it can be the popup itself.

- `run=` with `action=` or `args=` raises `TypeError` before anything touches the page, and so do a `run=` that isn't callable, `heal.interrupt(key, None, run=...)` and, on the sync healer, an async function. On `mendwright.aio.open()` it is an async function, which is awaited on the event loop, or a plain one that returns an awaitable; one that returns anything else raises `TypeError` when the interruption is handled.
- Its own actions never handle its own interruption, which is still on the page while it fills it in, so it is never called from inside itself. They do handle the other registered interruptions, such as a cookie banner that opens over its form. They dismiss no popup with `dismiss=True`, and neither does anything else while it runs. Playwright's overlay handlers stay out of them, as they stay out of mendwright's own clicks, and when Playwright's handler calls it, Playwright runs no handler inside it. On the async healer its calls go ahead without the healer's lock, which the step that called it holds while it waits.
- It is not part of the step `run_steps()` runs: the keys it looks up are not saved with that step.
- After it returns, mendwright waits for the element to go away, as after an action, and logs `handled interruption 'session_expired' before 'search' with relogin()`. The `flow.interrupt` event has `"action": "run"` and `"function": "relogin"`, the function's `__name__`. One that is still there counts as handled all the same, so the usual limits stop it: a step handles at most 3, and the overlay handler leaves it alone after 3 times in a row.
- What it raises is raised as it is, after one WARNING and a `flow.interrupt` event with `"outcome": "failed"` and the error's type. A step that called it stops there. An `AttentionRequired` from it isn't taken for a miss of that step, so nothing is healed or skipped. When Playwright's overlay handler called it during a step mendwright routes, for a popup that opened while the step's action waited, the error can't reach that action, which goes on. The step raises the error once the action ends, without calling the function again. The action ends when it goes through or when its timeout runs out: with a popup that stays over the element, the step raises the error only after the action's whole timeout (the page's default timeout, unless the action has its own), and with one that goes away by itself, after its action went through. Before an action mendwright doesn't route, the error can't reach the action: the WARNING says so, and the overlay handler leaves the interruption alone until mendwright sees it gone or the page navigates.
- Playwright's overlay handler calls it only for a popup that opens while an action waits, and before an action mendwright doesn't route: it stays out of mendwright's own lookups, so a step calls the function itself, before its action, with no time limit. Inside the handler, Playwright gives the function the time of the action it interrupted (the page's default timeout, unless the action has its own). A function that takes longer makes that action give up while the function goes on. An action mendwright doesn't route then raises Playwright's `TimeoutError` at once. A step mendwright routes waits for the function first, and never calls it a second time while it runs. When the action that gave up was its own, the step then raises what the function raised, or, when the function returned, that `TimeoutError`. On newer Playwright releases, 1.55 among them, the function's own actions after that point wait for the handler, which is the function itself, and fail with `TimeoutError`. So keep a function that can be called this way within that timeout, or raise the timeout.
- On `mendwright.aio.open()`, cancelling the step cancels the function too, for example when `asyncio.wait_for()` gives up on the step: one WARNING says the function was cancelled and that the interruption may still be on the page, partly filled in, and the `flow.interrupt` event has `"outcome": "failed"` and `"error": "CancelledError"`.

### Cookie banners

https://github.com/user-attachments/assets/d2d067eb-c394-42ca-98e7-6c1645246551

`mendwright.open(page, cookies="reject")` registers an interruption under the key `cookie_banner` that closes consent banners without accepting. `cookies="accept"` accepts them instead. It is off by default, so mendwright never clicks anything you didn't ask for.

It knows the reject, decline, necessary-only and close buttons (or the accept buttons) of these consent managers by their ids and classes: OneTrust, Cookiebot, Didomi, TrustArc, Usercentrics, Quantcast Choice, Osano, the cookieconsent library (Osano's and orestbida's), CookieYes (and its older Cookie Law Info plugin) and Complianz.
For other banners there is a fallback: a button or link whose text, aria-label or title says reject, decline, deny, disagree, necessary only, essential only or continue without accepting (for `"reject"`), or accept, allow or agree (for `"accept"`), inside a cookie banner. With `"reject"`, a Close button counts too, but only when its whole label is Close, or Close followed by what it closes, the banner itself (Close banner, Close this dialog, Close the popup and the like, listed as `CLOSE_NAMES` in `mendwright/interruptions.py`), or when it is an icon whose aria-label (or, without one, title) is one of those. So a button such as Close account never counts, as text or as an icon's name.

The cookie banner is one element that meets all of these:

- its id, class or aria label mentions cookie or consent
- its text mentions cookies
- its text is at most 1,500 characters
- it holds no `main` or `nav` element and no text field
- it isn't `html`, `body` or `main`

So a consent screen that isn't about cookies, such as an OAuth screen with Allow and Deny or a terms step with I agree, is never clicked, and neither is a page-wide wrapper with a class such as `cookie-consent-given`.
The selectors are XPath, so they run on Playwright and Selenium. Usercentrics draws its banner in a shadow root, which XPath can't reach, so its buttons are also looked for with CSS, which Playwright reaches and Selenium doesn't.
The selector lists are in `mendwright/interruptions.py`.

If your script has its own step for the banner, register that key with `heal.interrupt()` instead of using the preset, so the step is skipped once the banner is gone. A preset and a step of your own for the same banner can get in each other's way, and on Playwright a step that reaches the banner through `get_by_role()` or another locator mendwright does not route finds it closed already (see [Interruptions](#interruptions)). On Selenium such a step goes ahead: nothing covers the banner's own buttons, so mendwright leaves the banner to it.

### Popups mendwright wasn't told about

https://github.com/user-attachments/assets/71f4a9e2-9e89-4da5-84d2-05d74fca0c71

```python
heal = mendwright.open(page, dismiss=True)

heal.locator("email", "#email").fill("ada@example.com")   # a newsletter modal opened first
```

`mendwright.open(page, dismiss=True)` dismisses a popup that nobody registered when it is in the way of an action: a newsletter or discount modal, a prompt for your address or location that can be put off, an app-install banner, a survey, a chat invite, or a consent banner when `cookies=` isn't set.
It is off by default, and with `dismiss=False` mendwright makes no browser call for it.

mendwright acts only when the action's element is covered:

- before an action, when the middle of the element is inside the window and the browser says another element is on top there (`document.elementFromPoint`, into open shadow roots): not the element, not inside it, and not one of its containers. An element below the window is scrolled into view first, as the action would scroll it.
- when an action fails because something covered the element (the same errors as for [interruptions](#interruptions)), after which it looks in the same way

[Registered interruptions](#interruptions) come first.
Before an action, mendwright handles those the page shows, and then looks for a popup over the element.
While an interruption it handled for the action is still on the page, such as a banner that fades out slowly, it leaves popups alone: the popup over the element may be that banner, and declining it could undo what its action did.
After an action fails because something covered its element, mendwright handles the registered interruptions the page shows, and looks for a popup only when none of them is there.

The element on top belongs to a popup when it is in a dialog (`role="dialog"` or `"alertdialog"`, `aria-modal="true"`, or an open `<dialog>`). The nearest one is the popup. Otherwise the popup is the outermost fixed or sticky element it is in, other than `body`, that takes pointer events, and only when that element covers at least a quarter of the window.
mendwright leaves these alone:

- a popup that holds the action's element: you are using it
- a fixed header, footer or toolbar over the element, which covers less than a quarter of the window. Playwright scrolls to get around one, and the action's own error stands.
- a small toast in a full-window layer with `pointer-events: none`, as toast libraries and Angular's CDK overlay container draw them. The layer is left out, so the toast is measured on its own.
- an element on top that is in neither, such as an absolutely positioned one
- an alert dialog (`role="alertdialog"`, or a popup inside one). By its ARIA definition it asks for an answer, such as "Delete this project?" or "Close your position at market price?", and Escape or any of its buttons would give one, so mendwright presses and clicks nothing on it and logs a WARNING. That holds for a confirmation your own script opened too.
- with `cookies="accept"`, a popup that looks like a consent banner the preset doesn't know: its text says cookie, consent, "value your privacy", partners, personal data or trackers (and their German, French, Spanish and Italian words, such as Datenschutz and traceurs), its id, class or aria-label says cookie, consent, gdpr, privacy, dsgvo, rgpd, cmp, tcf or a consent manager's name, or it has a button that says accept, agree, allow or consent, in English or another language (akzeptieren, accepter, aceptar, accettare, zustimmen and others). A close button or a decline button could turn the consent down, the opposite of what you asked for, so mendwright logs a WARNING instead. Register its accept button with `heal.interrupt()`. With `cookies="reject"`, such a banner is declined like any popup.

For a popup, mendwright tries these in order, and after each looks again for up to 2 seconds (or the step's `timeout_ms`, when that is shorter) until the popup is gone or hidden or the element is on top again:

1. It presses Escape, but only while the keyboard focus is on the page's body or in the popup, not inside an iframe, and never with `cookies="accept"`. Escape goes to the element that has the focus, wherever it is: in a search field your script filled it clears the field, and it closes the page's own dialogs. In an iframe, the focus may be in the page around it, which mendwright never looked at. And every key listener of the page gets the key, not only the popup's: a consent banner elsewhere on the page, or behind a backdrop of its own, may take it as a refusal.
2. It clicks the popup's close button: one whose whole label is Close, or Close followed by what it closes, the popup itself (Close dialog, Close this banner, Close the popup, Close cart, Close newsletter and the like, listed as `CLOSE_NAMES` in `mendwright/interruptions.py`), or an icon whose aria-label (or, without one, title) is one of those. A bare ×, ✕ or X with no such name is never one: in a cart, a list of filters or an upload panel it removes one row, and X is also a network to share on. Neither is an icon named Close position, Close ticket or Close account.
3. It clicks a button of the popup that declines: one whose label says no thanks (or no thank you), not now, maybe later, later, skip, skip for now, dismiss or not interested, or says continue without and what it leaves out (accepting, accepting all, accepting cookies, agreeing, an account, account, signing in, signing up, logging in, registering, subscribing or cookies). In a popup whose text speaks of cookies, reject, reject all and decline count too. Elsewhere they answer a request, such as an expense report to approve, an invitation to a workspace or new terms, so they never count there. Skip, skip for now and dismiss count only in a popup whose own text (its text outside its buttons, and its aria-label) reads as a promotion or onboarding: it says newsletter, subscribe, sign up for, offer, discount, % off (as in 10% off), sale, deal, coupon, promo or promotion, free shipping, download, install or get the app (or our app, or the Acme app), turn on, enable, allow, push or get notifications, survey, feedback, rate us, tour, tips, what's new, welcome (but not welcome back), getting started or chat with us, or it speaks of cookies or consent. Elsewhere a bare Skip or Dismiss may acknowledge something that matters, such as Dismiss in "New sign-in detected" or Skip in "Update your payment method", so it never counts there. These are whole words: "Your subscription payment failed" doesn't say subscribe, and notifications alone doesn't count, since Dismiss all in a notification centre clears its alerts. The label may have up to 4 other words, from a short list that changes nothing: "No thanks, I'd rather not", "Remind me later" and "Not now, thanks" decline, and "Skip the line", "Thanks, I'm in", "Decline offer" and "Continue without delay" don't.

A popup that asks a question gets Escape and nothing more. That is one with a button whose label says cancel or keep, such as Cancel, Keep it, Keep open or Keep my plan: its other buttons, No thanks too, may be the answer. And a close, skip or dismiss button whose verb the popup's own text says (its text outside its buttons, and its aria-label) is never clicked: Close in "Close your account?", Skip in "Skip next week's delivery?", Dismiss in "Dismiss this alert for good?".

A button is a `<button>`, an element with `role="button"`, an `<input>` of type `button` or `submit`, or a link with no `href`, or with `#` or `javascript:` code that does nothing (`javascript:`, `javascript:void(0)` or `javascript:;`). Other `javascript:` code runs, and may load a page, so such a link is one that goes somewhere. An element with another role (radio, checkbox, switch, tab, option, link, menu item and so on), or with `aria-checked`, `aria-pressed` or `aria-selected`, is never a button: a click on it chooses or toggles something. What a button shows is its text as the page renders it (text hidden with `display: none` left out) and an input's value; for an icon that shows one character or none, its aria-label (or, without one, its title) stands in. The words are compared in lower case, with punctuation read as spaces, as for the [cookie banners](#cookie-banners), and in their compatibility form: a ligature such as ﬁ reads as fi, a fullwidth letter as the letter, any dash as a space, and a soft hyphen or a zero-width space as nothing. The first close button in page order is tried, and the first decline button whose label is a decline phrase and nothing else, such as No thanks, else the first decline button.

mendwright never:

- clicks a link that goes somewhere, a button inside such a link or one that holds one, or a disabled button
- clicks a button in a `<label>`, a label itself, or a button that holds a field: the click could check a box
- clicks a button that would submit or reset its form: a `<button>` in a form with no `type`, or with `type="submit"` or `"reset"`, or an `<input type="submit">` in a form, whatever the form's method. A form with `method="dialog"` sends nothing, but it still fires its `submit` event, and the page's handlers of it run. An `<input type="submit">` outside any form submits nothing.
- clicks a button whose label (its text, the text it hides, its value, aria-label, title, and the text of the elements its `aria-labelledby` names) says accept, agree, allow, subscribe, sign up, sign in, log in, join, register, yes, ok, confirm, continue (unless it says continue without and what it leaves out), buy, pay, order, save, submit, delete, remove, install, enable, notify, start, get, claim or try, or one of these words with the same risk: signup, signin, login, unsubscribe, subscription, registration, submission, trial, bought, paid, sent, discard, create, purchase, checkout, cancel, send, share, download, upgrade, unlock, redeem, apply, book, donate, follow, open, next, proceed, verify, activate, connect, log out, logout, sign out or leave. Each matches as the start of a word, without a last e or y, so pay also catches payment and paying, save catches saving, and notify catches notification. So "No thanks, I'll pay full price", "Skip to payment" and "Continue without saving" are never clicked.
- clicks a button whose label has a digit or a currency sign, such as "Skip the line - $9.99" or "No, I want 10% off"
- types, checks a box or selects an option, or clicks a radio, checkbox, switch, tab or option, whatever its tag
- presses Escape or clicks anything on an alert dialog
- acts on a popup only because it is visible somewhere on the page

When a popup was dismissed, mendwright logs one WARNING line, writes a `flow.dismiss` journal event, lists it in `heal.handled_interruptions` as `InterruptionEvent("<popup>", before, popup=..., how=..., label=...)`, and goes on with the action, or tries a failed one once more, as after an interruption. `popup` names it as the WARNING does, `how` is `"escape"`, `"close"` or `"decline"`, and `label` is the label of the button that closed it (`None` for Escape).
Each popup counts once toward the 3 interruptions a step may handle, whatever was tried on it. A popup that comes back each time it is dismissed raises `InterruptionLoop`, an `AttentionRequired`, once the step has handled 3, and so does a popup still in the way after the step handled 3 registered interruptions.

When nothing worked, mendwright logs one WARNING that names the popup, says what it pressed and clicked, and says how to handle it. It doesn't try again in that step, and writes a `flow.dismiss` event with `outcome` `failed` when it pressed or clicked anything. The action goes ahead as it would without mendwright, and if the popup blocks it, the browser's own error is raised and the action isn't tried again.
A popup that takes longer to close than the time mendwright looks again for, such as one with a long exit animation, is reported this way too, though it may close afterwards: mendwright looks once more before it gives up, and no longer than that.
When the action's element can't be looked at as before after one of the steps, mendwright can't tell what that step did. That is when the element matches no element or several now (a button submitted a form and the page changed), or is hidden or outside the window (Escape closed the page's own dialog that held it). It stops there with a WARNING that says so, writes a `flow.dismiss` event with `outcome` `lost`, doesn't count the popup as dismissed, and doesn't try a failed action again.
A click that raises an error after it may have reached the page is handled the same way: a close button that starts a navigation that doesn't commit within the click's timeout, for one. The WARNING names the button, the event's `tried` lists the click, and nothing more is clicked.

What it can't do:

- A popup that needs data before it closes, such as a postcode prompt with no skip or a sign-in after the session expired. Register a function that fills it in with `heal.interrupt("postcode", "#postcode-prompt", run=fill_postcode)` (see [Interruptions that need a form](#interruptions-that-need-a-form)). For one field and a button, registering the field and then the button works too, since they are handled in that order: `heal.interrupt("postcode", "#postcode", action="fill", args=("10115",))`, then `heal.interrupt("postcode_ok", "#postcode-ok")`.
- A popup whose only way out is a button mendwright never clicks, such as Subscribe or OK, or a bare Skip or Dismiss in a popup that doesn't read as a promotion or onboarding. Register the button you want with `heal.interrupt()`.
- A backdrop that is a separate element from the popup's buttons, as some consent managers draw one. Only Escape is tried there, and with `cookies="accept"` not even that. Use `cookies=` or `heal.interrupt()` for those.
- See what a button's own script does. A close or decline button whose click handler loads another page, sends a request or starts a trial is clicked like any other: the `lost` WARNING, when the page changed under the element, is the only sign. Escape, too, reaches every key listener of the page.
- A popup of the page over an iframe that holds the element. mendwright looks in the element's own document, so a popup inside the iframe is handled, and one outside it isn't. Inside an iframe Escape is never pressed, so a popup there that only Escape closes stays.
- A popup that opens while Playwright waits on an action is found only when the action fails, after its timeout.
- A popup that hides the element instead of covering it, such as a wall that replaces the page until you answer. The lookup then misses, and mendwright looks for popups only over an element it found. Register it with `heal.interrupt()`, which is also handled when a lookup misses.
- Actions that don't go through `heal.locator()`, `heal.page` or `heal.driver`. The page or driver you passed to `open()`, and the locators you build from it, get no popup check, and neither do `page.mouse`, `page.keyboard`, `dispatch_event()`, `ElementHandle`s from `query_selector()` or `wait_for_selector()`, `Frame`s from `page.frame()` or `page.frames`, the locators `Locator.all()` returns (but for `heal.page.locator(selector).all()`'s), page methods that aren't `heal.page` shortcuts (such as `tap()`, `set_checked()` or `drag_and_drop()`), and a shortcut whose selector mendwright can't store. On Selenium, `ActionChains`, and the elements that `execute_script()` or `switch_to.active_element` return, get none. Playwright's overlay handler, which also handles [interruptions](#interruptions) before such actions, isn't used for popups.

On Playwright, with `dismiss=True`, the locators `heal.page` doesn't heal are guarded too: `get_by_role()` and the other `get_by_*` locators, a locator built with options, chained locators, `frame_locator()`, `and_()`, `or_()`, `.first`, `.last`, `.nth()` and `.filter()`, and the locators `heal.page.locator(selector).all()` gives.
They come back as `GuardedLocator`s, the real locators with the checks above and the interruption checks before their actions. They aren't healed and nothing about them is saved. They pass `isinstance()` checks, so `expect()` takes them, and `.native` is the real locator. Log lines name their step after the calls that built it, such as `"page.get_by_role('button', name='Continue')"`. With `dismiss=False` they are the real locators, as before.
With `heal_get_by=True`, the `get_by_*` calls of `heal.page` are no longer `GuardedLocator`s: they heal, and their actions get the same checks, logged before the call's key (see [get_by locators](#get_by-locators)). The locators chained from them still are, with the names above.
On Selenium, the elements `heal.driver` finds are covered whatever `By` found them (see [No changes per step](#no-changes-per-step)), inside iframes too.

The look costs one browser round trip before each action, and none before `focus()` and `set_input_files()`, which work on a covered element.
On Selenium it runs inside the element's iframe, with the implicit wait at 0 while mendwright finds the iframe, and Escape is sent with `ActionChains`. The async healer awaits the look on the event loop, and dismisses a popup in a worker thread, as it handles interruptions.
A dismissed popup isn't a heal: `--mendwright-strict` doesn't fail a test because of one, as it doesn't for a handled interruption.

### Optional steps

```python
promo = heal.locator("promo_close", "#promo-close", optional=True)
promo.click()          # does nothing when the promotion isn't shown
if promo.present:
    print("closed the promotion")
```

With `optional=True`, a step whose element doesn't appear within `timeout_ms` isn't healed. You get a proxy whose `.present` is False, whose actions do nothing and return `None`, and whose `.native` is `None`. Reading from it, for example `promo.inner_text()`, raises `AttributeError`. One INFO line is logged and a `flow.skip` journal event is written. An optional step is never healed, even when its selector broke.

### Steps that moved to another page

mendwright saves the page where each key's element was found, in the store next to its selectors: `lastPage` (the most recent) and `pages` (the last 5). A page is its path in lower case, without the scheme, host, query or fragment, and without empty parts, so a trailing or doubled slash changes nothing. Parts that look like record ids are replaced by `*`: numbers, uuids, hex of 16 or more characters such as a MongoDB id, and generated ids of 16 or more letters and digits (dashes and underscores allowed) with at least two digits, such as a ULID. So `https://shop.example/Orders/10482/?tab=2` is `/orders/*`, and so is `/orders/507f1f77bcf86cd799439011`. Short parts such as `v2`, `2fa` or `step-3` stay as they are. `about:blank`, `data:` URLs and the site root have no page, and are never compared. A `file://` page is compared by its path, so two local files, such as `v1.html` and `v2.html` of the same page, count as two pages. Next to `lastPage` the store saves `lastUrl`, the page's address with its record ids masked, for [`mendwright check`](#checking-for-drift-before-a-run); the page guard doesn't use it.

When a key's selector misses on a page that isn't one of its saved pages, mendwright declines the heal before any model call, raises `AttentionRequired` and writes a `flow.page_guard` journal event:

```text
'continue' was last found on /signup, and this page is /signup/details. That looks like a change in the flow, not a renamed element, so mendwright did not heal it. If the step moved, update your script. If the URL itself changed in a redesign, pass heal_across_pages=True to mendwright.open().
```

If a redesign only moved the page to a new URL, pass `heal_across_pages=True`. Pass it too when the same steps run under several language paths, such as `/en/checkout` and `/de/checkout`, because those are different pages to the guard. Inside [`heal.run_steps()`](#steps-in-any-order), a key found during the step that runs heals on the page that step showed on, whatever its address. A key that is used on several pages, such as a search box in the header, heals on any of the last 5 pages it was found on. When two processes share a store, as with pytest-xdist, the pages both of them found a key on are kept.

The guard needs a page on both sides, so it doesn't cover a key on its first run, pages loaded with `page.set_content()` (they are `about:blank`), or a step that moved away while the path stays the same. That last case covers apps that route with the URL fragment (`/#/signup`) or the query string (`?page=signup`), and apps that never change the URL. A single-page app that changes the path with the History API is covered. In those cases the model is told it may say that no element fits, and gets the current page and the last page the key was found on when they are known.

### Steps in any order

https://github.com/user-attachments/assets/42818d17-e419-4f5b-80f2-595c1d080c7b

```python
heal = mendwright.open(page)

@heal.step("shipping", when="#ship-name")      # what shows this step is on the page
def shipping(page): page.fill("#ship-name", "Ada"); page.click("#next")

@heal.step("payment", when="#card-number")
def payment(page): page.fill("#card-number", "4242 4242 4242 4242"); page.click("#next")

@heal.step("review", when="text=Place order", last=True)
def review(page): page.click("text=Place order")

heal.run_steps()   # runs whichever step the site shows, each once, in any order, until the last one
```

The ids keep the example short. In a real flow, use something such as a heading as `when`, so that a renamed field still heals inside its step: a landmark is never healed.

`heal.step(name, when=None, fn=None, *, url=None, last=False, optional=False)` registers one step of a flow, such as a checkout, a signup or a booking, whose steps the site may show in another order, put on one page, or leave out. Use it as a decorator, or pass the function: `heal.step("payment", "#card-number", payment)`. It returns the function.

- `when` is what shows the step is on the page, its landmark: one selector, or a list of them, and any visible match counts. A landmark that matches several elements counts when one of the first 5 is visible. On Selenium a CSS selector that matches several counts when the first is visible, and an XPath is looked at match by match. When the step doesn't show, a landmark that matches more elements than mendwright looks at, none of those visible, is named in one WARNING per step and page, with selectors that match one visible element when exactly one of its matches is visible, as for an [interruption](#interruptions): `step 'payment' did not show: its landmark '.step-title' matches 2 elements, and on Selenium mendwright asks only whether the first of them is visible, for a CSS selector, and it is not.`
- `url` is a pattern for the page's path, such as `"/checkout/payment"` or `"*/payment*"`, instead of a landmark or as one more condition with it. It and the page's path are normalized as for the [page guard](#steps-that-moved-to-another-page): lower case, without empty parts, and parts that look like record ids read as `*`, so `"/orders/123/pay"` matches `/orders/987/pay`. `*` matches any characters, `/` included. A pattern that starts with `/` starts at the root of the path, and one that starts with `*` is kept as written, so `"*/payment*"` matches `/payment` and `/checkout/payment`. A page with no path, such as `about:blank` or the site root, matches no pattern.
- `last=True` marks the step that ends the flow, such as placing the order. A flow has at most one, and it can't be optional. Give it to the final step of any flow that ends in a submit: then a step the site left out is named in a WARNING once the flow ends. With no last step, `run_steps()` waits for every step that isn't optional, so a step the site left out stops it with an error, after the steps that did show ran.
- `optional=True` marks a step that may not show at all, such as an upsell.
- The function gets `heal.page` on Playwright and `heal.driver` on Selenium, so its selectors heal as usual.
- Registering a name again replaces its step, which keeps its place in the order.
- Steps stay on the healer for every later `run_steps()` call, so the same flow can run again. For another flow, open a new healer with `mendwright.open()`.
- Arguments that can never work raise `TypeError`, `ValueError` or `InvalidSelector` at once, before anything touches the page. On Selenium that includes a Playwright selector in `when`, such as `text=Place order`.

`heal.run_steps(*, timeout_ms=None, poll_ms=200)` runs the steps and returns their names in the order they ran:

1. It waits, up to `timeout_ms` (by default the healer's), until a step that hasn't run yet shows: its landmark is visible, its URL pattern matches, or both when it has both.
2. It runs that step's function.
3. It looks again. It stops after the last step ran. With no last step, it stops once every step that isn't optional ran, so an optional step that comes after them isn't waited for.

Each step runs at most once. When several steps that haven't run show at the same time, as on a one-page checkout, they run in the order they were registered, and `run_steps()` looks again after each. The last step runs only when no other step that hasn't run shows, wherever it was registered.

Landmarks are never healed, and nothing is saved for them: a missing landmark is what a step that isn't on the page looks like. Pick ones that stay, such as a heading's text or a field's name, and that only that step's page shows. A landmark that every page shows, such as a Place order button in a sidebar, makes its step run as soon as no other step shows.

While it waits, `run_steps()` handles the [interruptions](#interruptions) you registered, and `cookies=`, when the page shows them, as when a `locator()` misses. So a consent wall that hides the page until you answer doesn't stop the flow. The log line and the `flow.interrupt` event name the step as `'<next step>'`. It handles up to 3 interruptions in each wait, as for one action. An interruption whose action fails is named in one WARNING and not tried again in that wait, and the others still are, so a consent wall that loads late is still answered. With `dismiss=True`, a [popup](#popups-mendwright-wasnt-told-about) is dismissed by the step's own actions, as everywhere else: a popup over a landmark doesn't hide it, so the step runs, and its first action finds the popup in its way.

When it can't tell what to do, `run_steps()` stops:

- A step that ran and is on the page again, with no step that hasn't run for the whole `timeout_ms`, raises `AttentionRequired`. That is most often a submit that failed, such as a declined card, and running the step again could pay twice:

  ```text
  mendwright: 'payment' is on the page again after it ran, and no step that has not run showed on /checkout/payment within 10000 ms: its submit probably failed, for example because the card was declined or the site rejected a field. mendwright runs each step once, so it stopped instead of running 'payment' again. Look at the page for the site's message. Steps that ran: 'shipping', 'payment'.
  ```

- A step that ran and shows again on another page, with no step that hasn't run for the whole `timeout_ms`, raises `AttentionRequired` with other advice. The page changed, so its submit most likely worked, and this is most often a step the site added that shows part of it, such as gift options under the Shipping heading:

  ```text
  mendwright: 'shipping' shows on /checkout/gift, and it ran on /checkout/shipping, so the page changed after it ran, and no step that has not run showed within 10000 ms. This is probably a step the site added that shows part of 'shipping', such as its heading. If it is, add it with heal.step(): @heal.step("<name>", "<selector of something only that step shows>"), or give 'shipping' a landmark only its own page shows. If its submit failed instead, look at the page for the site's message. mendwright runs each step once, so it stopped instead of running 'shipping' again. Steps that ran: 'shipping'.
  ```

- A page where no step shows within `timeout_ms` raises `AttentionRequired` with the page and every landmark it looked for. A step the site added, such as gift options, needs a `heal.step()` of its own:

  ```text
  mendwright: no step showed on /checkout/gift within 10000 ms. mendwright looked for 'payment' (#card-number) and 'review' (text=Place order). Steps that ran: 'shipping'. If the site added a step here, add it with heal.step(): @heal.step("<name>", "<selector of something only that step shows>"). If a step's landmark changed, update its when=: landmarks are never healed.
  ```

  When steps ran and none has `last=True`, the message also says that the site may have dropped a step, and to mark the step that ends the flow with `last=True`.

- An error in a step's function is raised as it is, after the journal records which step failed.
- It runs at most 3 times as many steps as were registered when it started. A step function that keeps registering new steps could otherwise keep the flow going.

Inside a step, a key's page is the step, not its URL. mendwright saves with each key the steps it was found during, as `steps` in the store next to `pages`. While a step runs, a key found during that step before heals on the page the step showed on when it started, whatever its address, because the step's landmark, or its `url`, showed there that this is that step's page. So a field renamed in a step that also moved to another page heals there, where the [page guard](#steps-that-moved-to-another-page) would decline it, and one INFO line says so. Once the step's function goes to another page, such as with its own Continue, that page may be the next step's, and the guard is as before there: a lookup that misses after the step's submit is not healed onto the next step's controls. For the same reason, a key is saved with a step only when it was found on the page the step showed on. A key never found during the step that runs is guarded as before, and so is every key outside `run_steps()`.

What it reports:

- one INFO line for each step it ran, with the page and the time it took
- one WARNING when a step runs after steps registered behind it: `ran 'payment' before 'shipping': the site changed the order of the steps`
- once the last step ran, one WARNING for each step that isn't optional and never showed, since the site may have dropped it, and one INFO line for each optional step that didn't show
- a `flow.step` journal event for each step that ran, failed or never showed (see [Log lines and journal events](#log-lines-and-journal-events))

None of this is a heal, so `--mendwright-strict` doesn't fail a test because the steps came in another order, as it doesn't for a handled interruption. A heal inside a step still fails it.

### When the model finds no element

The prompt tells the model it may answer `{"xpath": null, "reason": "..."}` when no element on the page is the one the step means, and that it must not pick an element that is only similar. That answer is not a heal. The error says:

```text
AnthropicProvider returned no selector for 'continue': the model says no element on this page fits: The Continue button is not on this page. If the step moved to another page or the element was removed, update your script.
```

Through a gateway, the reference worker in `examples/heal_gateway.mjs` passes the same reason on in its `needs_human` reply.

### Log lines and journal events

```text
WARNING mendwright: handled interruption 'cookies' before 'email'
WARNING mendwright: handled interruption 'cookie_banner' before '<page action>'
WARNING mendwright: handled interruption 'session_expired' before 'search' with relogin()
WARNING mendwright: relogin(), the run= function of interruption 'session_expired', raised an error while mendwright handled it before 'search' (LookupError: no password for ada in the vault). The error is raised as it is.
WARNING mendwright: relogin(), the run= function of interruption 'session_expired', was cancelled while mendwright handled it before 'search', because that step was cancelled. 'session_expired' may still be on the page, partly filled in.
WARNING mendwright: interruption 'cookies' was skipped: its selector '.cookie-accept' matches 2 elements, and mendwright acts on a CSS or Playwright selector only when it matches one (an XPath is narrowed to its first visible match). Use a selector that matches only the element to act on, such as '#accept-all' or 'role=button[name="Accept all"]', which each match one visible element on this page.
WARNING mendwright: dismissed a popup in the way of 'email': <div role="dialog" class="nl"> ("Get 10% off your first order. Sign up No thanks") with “No thanks”, after Escape did not close it
WARNING mendwright: left a popup in the way of 'go' alone: <div id="cf" role="alertdialog"> ("Delete this project? Cancel Delete"). It is an alert dialog (role="alertdialog"), which asks for an answer, such as "Delete this project?", and Escape or any of its buttons would give one, so mendwright does not press or click anything on it. The action goes ahead, and fails with the browser's own error if the dialog blocks it. If your script should answer it, register the button with heal.interrupt("<name>", "<selector of the button>").
WARNING mendwright: could not dismiss a popup in the way of 'create': <div id="promo" role="dialog"> ("Join our list. Subscribe Yes, sign me up"). mendwright pressed Escape, which did not close it, and it has no close or decline button that mendwright may click. The action goes ahead, and fails with the browser's own error if the popup blocks it. Register the button that closes it with heal.interrupt("<name>", "<selector of the button>"). If it needs data first, register its field before the button: heal.interrupt("<name>_field", "<selector of the field>", action="fill", args=("<value>",)).
WARNING mendwright: the action on 'email' failed because something covered its element or it could not be reached, and none of the interruptions mendwright looks for (heal.interrupt(): 'cookies') matched a visible element, so it did not try the action again. If the element in the way is one of those registered with heal.interrupt(), its selector no longer matches: update it. Otherwise register the button that closes the element in the way with heal.interrupt().
INFO mendwright: skipped 'cookies': it was already handled as an interruption before 'email', and it is not on the page now.
INFO mendwright: skipped 'promo_close': it is optional (optional=True) and is not on the page.
INFO mendwright: ran step 'payment' on /checkout/step-a (0.4s)
WARNING mendwright: ran 'payment' before 'shipping': the site changed the order of the steps
WARNING mendwright: 'gift' never showed before the flow ended with 'review': the site may have dropped it. If it did, delete its heal.step(). If it shows only sometimes, pass optional=True to heal.step('gift', ...).
INFO mendwright: the page guard lets '#ship-name' heal on /checkout/step-b: it was found during step 'shipping' before (last on /checkout/step-a), and this is the page 'shipping' showed on
```

| Event | Fields |
|---|---|
| `flow.interrupt` | `key`, `before` (the step it was handled for, `<page action>`, or `<next step>` while `run_steps()` waits), `action` (`run` for a `run=` function), `selector` or `preset`, `function` (the `run=` function's `__name__`), and `outcome` `failed` with `error` (the error's type) when a `run=` function raised |
| `flow.dismiss` | `key` (`<popup>`), `before` (the step the popup was in the way of), `popup` (as the WARNING names it), `outcome` (`dismissed`, `failed` when mendwright pressed or clicked something and the popup stayed, or `lost` when the element could not be looked at after a step, or a click raised an error after it may have reached the page), `how` (`escape`, `close` or `decline`, for `dismissed`), `label` (the button's, for `close` and `decline`), `tried` (the steps that did not close it, in order, such as `[{"how": "escape"}, {"how": "close", "label": "Close"}]`: for `dismissed`, those before the one that did) |
| `flow.skip` | `key`, `reason`: `optional`, `interruption handled` or `interruption absent` |
| `flow.page_guard` | `key`, `last_page`, `page` |
| `flow.step` | `key` (the step's name), `outcome` (`ran`, `failed` when its function raised, or `missing` for a step that isn't optional and never showed), `error` (the error's type, for `failed`), `position` (1 for the first step that ran), `registered` (its place in the order the steps were registered), `page`, `seconds` |

## Selectors

`locator()` takes three kinds of selector, decided by `mendwright.selectors.classify()`:

| Kind | Recognised by | Runs on |
|---|---|---|
| XPath | starts with `/`, `(`, `./` or `..`, or has an `xpath=` prefix | Playwright and Selenium |
| Playwright selector | an engine prefix such as `role=`, `text=`, `data-testid=` or `internal:`, or a ` >> ` chain | Playwright only |
| CSS | anything else, or a `css=` prefix | Playwright and Selenium |

An XPath that starts differently, such as `id('main')//button`, needs the `xpath=` prefix, or it is read as CSS.

A list of selectors is tried in order before any model is asked.
The selectors in your code always come first. When you change them, the old ones get status `superseded`: they stay in the file and are no longer tried.
Heals stay behind your selectors as fallbacks.

A malformed selector raises `InvalidSelector` at once instead of waiting out the timeout:

```text
mendwright: the selector for 'x' is not usable: '//input[' is not valid XPath (lxml says: Invalid expression). Check its brackets and quotes; you can test a fix with mendwright.selectors.check_syntax().
```

`mendwright.selectors.check_syntax(selector)` checks one without a browser.
Passing a Playwright `Locator` instead of a string raises `TypeError`, because a Locator can't be stored.

Heals are XPath, because the gate checks them with lxml. When a key's selector in your code is CSS, mendwright also looks for a CSS selector for the healed element and saves it with the heal as `css`: `#id`, a test id, `tag[attr="value"]`, `tag.class`, two of these together, or one inside a container with an id, test id, name, aria-label or class. It must match that element and nothing else on the captured page, and on the live page it must match one element with the same outer HTML. `mendwright report` shows it, and `mendwright fix` writes it wherever your code has CSS. CSS can't match text, so an element that only its text tells apart has no CSS form, and a Playwright engine selector (`text=`, `role=`) gets the XPath.

## Roles

A proposal is accepted only if the element it finds has the step's role, both in the captured HTML and again on the live page.
Roles use mendwright's own vocabulary: `button`, `checkbox`, `radio`, `link`, `input` and `combobox`, or the tag name for anything else (`textarea`, `div`, `h1`).

The check reads an element like this:

- a `role` attribute of checkbox, button, link, combobox or radio
- `input` with type checkbox, radio, submit or button gives checkbox, radio or button; any other input is `input`
- `button` is button, `a` is link, `select` is combobox
- anything else is its tag name

`role=` also accepts ARIA names and maps them:

| You pass | Means |
|---|---|
| `textbox` | input or textarea |
| `searchbox`, `spinbutton`, `slider` | input |
| `listbox`, `select`, `dropdown` | combobox |
| `switch` | checkbox or button |
| `a` | link |
| `image`, `img` | img |
| `listitem`, `list` | li, or ul or ol |
| `heading` | h1 to h6 |
| `paragraph`, `navigation`, `row`, `cell`, `columnheader` | p, nav, tr, td, th |

HTML tag names and custom element names (with a `-`) are accepted too. Anything else raises `ValueError`.
The same mapping applies when a proposal is checked, so a key saved as `textbox` accepts an `<input>` or a `<textarea>`.
An alias with several meanings, such as `switch`, accepts any of them; the element on the page only has to be one.

With `role=` left out, mendwright reads the role from the element the first time a selector matches, and saves it as `expectedRole`.
Before that it guesses from the selector text: `//select` gives combobox, `//input[@type='checkbox']` gives checkbox, `//input[@id='terms']` gives input, and `#terms` gives nothing.
A guess can be wrong. If `//input[@id='terms']` is really a checkbox, a heal that finds the checkbox is rejected for its role.
If a selector is already broken on its very first run and the role is unknown, the heal is declined:

```text
No matching element for: newsletter (tried 1 candidate(s)).
  -> mendwright does not know what kind of element 'newsletter' is (its expected role is unknown), so it cannot check a repaired selector. Pass role= to heal.locator(), for example role='button', or set expectedRole for the key in the seed file.
```

If `role=` disagrees with the element it matched, mendwright logs a WARNING, because a heal for that key would be rejected.

## Destructive controls

A key is destructive, and never healed, when you mark it in one of these ways:

- `destructive=True` in `heal.locator()`. It is saved in the store, so later runs keep it.
- the key is in `mendwright.open(destructive_keys=...)`
- the seed file marks it `"destructive": true`

```python
heal.locator("pay", "#pay", destructive=True).click()
```

A `heal.page.get_by_*` call has no `destructive=`: mark its key with `mendwright.get_by_key()` in `destructive_keys=`, or in a seed file. With the pytest `heal` fixture, which passes no `destructive_keys=`, use a seed file, or a `heal` fixture of your own in `conftest.py` (see [get_by locators](#get_by-locators)):

```python
heal = mendwright.open(page, heal_get_by=True,
                       destructive_keys={mendwright.get_by_key("get_by_role", "button", name="Pay")})
```

mendwright doesn't guess this from the key, from `text=` or from the page.
A key you don't mark can be healed onto any element that has its role, including a button that pays or deletes. Mark every step like that.

A marked step is refused before any model call:

```text
No matching element for: pay (tried 1 candidate(s)).
  -> 'pay' is marked as a destructive control, and mendwright never auto-heals those, because a validated but wrong selector here could act on a live account. Fix the selector in your code or seed file by hand.
```

If a heal was saved for the key before you marked it, mendwright doesn't act on it. It quarantines that selector and raises `AttentionRequired`.

`destructive=False` clears a flag that `destructive=True` saved.
It can't unmark a key from `destructive_keys` or the seed file.

## Store and journal

### overrides.json

The store is one JSON file, `./.mendwright/overrides.json` by default.
This is the file after a run that learned the role on the old page and healed the selector on the new one:

```json
{"schema":2,"selectors":{"accept_terms":{"expectedRole":"checkbox","candidates":[
  {"xpath":"#accept-terms","source":"code","addedAt":"2026-10-10T19:07:53+00:00","lastSucceededAt":"2026-10-10T19:07:53+00:00","status":"active"},
  {"xpath":"//label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox']","source":"heal","addedAt":"2026-10-10T19:07:53+00:00","lastSucceededAt":"2026-10-10T19:07:53+00:00","status":"active"}]}}}
```

The real file is a single line; it is wrapped here.

- `source` is `code` (passed to `locator()`), `heal` or `seed`.
- `status` is `active`, `quarantined` or `superseded`, or for a list heal that `heal.locator_all(..., nonempty=True)` found, `proposed` or `accepted` (see [Lists that always have items](#lists-that-always-have-items-nonemptytrue)). Only active candidates are tried.
- A list heal has `"list": true`, a `proposal` with what `mendwright lists` shows of it (when it was found, on which page, the selectors that matched nothing, where the list was and where the group is, two of their headings, containers and items, and an `id`), and once accepted an `acceptedAt`. A key keeps at most 3 of those that wait, apart from its other inactive candidates: the one you accepted, then the newest proposals, so newer proposals never drop an acceptance.
- Optional per-key fields: `expectedRole`, `expectedText` and `destructive`, `lastPage` and `pages`, the pages where the key's element was found (see [Steps that moved to another page](#steps-that-moved-to-another-page)), `lastUrl`, the address of the last of them, which [`mendwright check`](#checking-for-drift-before-a-run) loads again (scheme, host, port and path, with record ids, tokens, email addresses and long names as `*`, and no user info, query or fragment), `steps`, the steps of `run_steps()` it was found during, `intent` (see [Describing an element with intent=](#describing-an-element-with-intent)), `fingerprint`, what the element looked like the last time it matched (see [Heals without a model](#heals-without-a-model)), and `listFingerprint`, what the items of a list looked like the last time it matched two or more (see [Lists](#lists)), with a `label` (its heading) and a `place` (short hashes of where it sat) once a call with `nonempty=True` recorded it.
- A candidate inside an iframe has a `frameXPath` with the frame's selector.
- Up to 5 active candidates per key are kept, most recently successful first.
- The key of a `heal.page.get_by_*()` call is written as the call, and so is its `code` candidate: `"get_by_role(\"button\", name=\"Sign in\")"`, with the inner quotes escaped by JSON (see [get_by locators](#get_by-locators)). A seed file may hold such keys, to mark one destructive, say, and such candidates, which run as calls on Playwright and are dropped with an `InvalidSelector` on Selenium.

Writes take a lock file (`overrides.json.lock`), merge with what other processes wrote since the last read, and replace the file atomically.
On a conflict, `quarantined` wins, then `accepted`, and `destructive: true` wins.
If a write fails, mendwright logs one WARNING and keeps working in memory.

A file that is corrupt or can't be read raises an error when the store loads. It is never treated as empty, because the next write would replace it and lose every saved heal. Fix the file or move it aside.

To undo a heal by hand, delete its candidate or the whole key from the file.

### Seed files

`load_store(seed_path, override_path)` reads a seed file under the override file. `load_store(None, override_path)` means no seed.
The seed is never written. It uses the same schema, for example:

```json
{"schema": 2, "selectors": {
  "accept_terms": {"expectedRole": "checkbox",
                   "candidates": [{"xpath": "//input[@id='accept-terms']", "source": "seed"}]},
  "pay": {"expectedRole": "button", "destructive": true,
          "candidates": [{"xpath": "//button[@id='pay']", "source": "seed"}]}}}
```

A role and text set in the seed win over saved ones. Schema 1 files (`{"selectors": [{"key": ..., "xpath": ...}]}`) are converted when read.
Pass the result as `mendwright.open(page, store=load_store(...))`.

### journal.jsonl

Every heal step appends one JSON line to `journal.jsonl` next to the store:

```text
{"ts":"2026-10-10T19:07:53+00:00","event":"heal.requested","key":"accept_terms"}
{"ts":"2026-10-10T19:07:53+00:00","event":"heal.proposed","key":"accept_terms","xpath":"//label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox']","reasoning":"stub"}
{"ts":"2026-10-10T19:07:53+00:00","event":"heal.applied","key":"accept_terms","provider":"Stub","model":"stub-model","latency_ms":0,"xpath":"//label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox']","old":["#accept-terms"]}
{"ts":"2026-10-10T19:07:53+00:00","event":"heal.canary","key":"accept_terms","xpath":"//label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox']","success":true}
```

The events are `heal.requested`, `heal.proposed`, `heal.rejected` (with a `reason`), `heal.applied`, `heal.canary` and `heal.quarantine`, `heal.held` for a list that `heal.locator_all(..., nonempty=True)` found and saved for you to check (with its `xpath`, `css`, `id`, `items`, `label`, `differs`, `old` and `page`), and `heal.accepted` when you accepted one with `mendwright lists accept`, and for flow changes `flow.interrupt`, `flow.dismiss`, `flow.skip`, `flow.page_guard` and `flow.step` (see [Log lines and journal events](#log-lines-and-journal-events)).
`heal.proposed`, `heal.rejected` and `heal.applied` have a `source` of `fingerprint` for a heal from the [fingerprint tier](#heals-without-a-model) (its `provider` is `fingerprint` too) and `intent` for a key found from its [intent=](#describing-an-element-with-intent). `heal.applied` has a `source` and `provider` of `frame`, and a `frame` with the iframe's selector, for a key followed into an [iframe](#iframes). A model heal of a key that had a selector has no `source`.
A `heal.canary` or `heal.quarantine` that [`expect_value=`](#checking-what-a-read-returns) caused has a `reason`, such as `expect_value: inner_text() is not money`.
The events of a list looked for with `nonempty=True` have `"list": true`. A list heal you rejected, or one quarantined because the selector it replaced matched again, writes a `heal.quarantine` with a `reason` that says which.
A `heal.canary` says what confirmed or refused the heal in `by`: `test` when the test that made the heal passed (the pytest plugin, with the test's node id, up to 300 characters, in `test`), `expect` for `expect=` on an action, `expect_value` for [`expect_value=`](#checking-what-a-read-returns) on a read, `confirm` for `confirm()` or `confirm_all()`, `action` for an action that failed, and `destructive` for a heal of a key that was marked destructive after it. `mendwright fix` and the [GitHub Action](#open-a-pull-request-with-the-fixes) show it. A canary written before 0.9.0 has neither.
The journal holds selectors, reasons, the model's reasoning and timings. It holds no page HTML.
A failed journal write never breaks a heal.

## CI

### Keep heals between runs

On GitHub Actions, cache the store folder.
The key changes on every run so the cache is saved each time, and `restore-keys` picks up the newest one:

```yaml
- uses: actions/cache@v6
  with:
    path: .mendwright
    key: mendwright-${{ github.ref_name }}-${{ github.run_id }}
    restore-keys: mendwright-${{ github.ref_name }}-
- run: pytest
```

In Docker, keep the store on a volume:

```bash
docker run -v mendwright-store:/data -e MENDWRIGHT_STORE=/data/overrides.json my-tests
```

### Show heals in the job summary

```yaml
- name: mendwright report
  if: always()
  run: |
    if [ -f .mendwright/overrides.json ]; then
      mendwright report --format md >> "$GITHUB_STEP_SUMMARY"
    fi
```

`mendwright report` exits 1 when there is no store, which happens when no test used mendwright.

### Fail on drift

A cached store and strict mode work against each other: once a heal is cached, the next run resolves it and strict mode never fires.
Give a strict job its own fresh store:

```yaml
- run: pytest --mendwright-strict --mendwright-store "$RUNNER_TEMP/mendwright/overrides.json"
```

### Check before the nightly run

[`mendwright check`](#checking-for-drift-before-a-run) loads the page of each key in the cached store and says which keys broke, before the run starts:

```yaml
- uses: actions/cache/restore@v6
  with:
    path: .mendwright
    key: mendwright-${{ github.ref_name }}-${{ github.run_id }}
    restore-keys: mendwright-${{ github.ref_name }}-
- run: python -m playwright install chromium
- name: mendwright check
  run: mendwright check --base-url "$SHOP_URL" --storage-state check-auth.json --format md >> "$GITHUB_STEP_SUMMARY"
```

The step fails the job when `mendwright check` exits 1 (a key broke) or 3 (a key couldn't be checked). Put `if: always()` on later steps that should still run.

### Open a pull request with the fixes

mendwright's GitHub Action runs `mendwright fix --apply` for you in CI and opens a pull request with the result.
It runs in a job of its own after your test job, takes the store and journal from that job's artifact, changes the commit the tests ran on, and opens one pull request into the branch the tests ran on, on the branch `mendwright/fix/<base>`, or updates the one it opened before.
It writes only heals your tests confirmed, and the list heals you accepted with [`mendwright lists`](#mendwright-lists), never a `heal.page.get_by_*()` call. It never pushes over a commit it did not make, and it never merges, approves or turns on auto-merge: a person merges the pull request.

```yaml
name: tests

on:
  push:
    branches: [main]
  schedule:
    - cron: "0 5 * * *"
  workflow_dispatch:

jobs:
  test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@<sha>  # v7.0.1
        with:
          persist-credentials: false
      - uses: actions/setup-python@<sha>  # v7.0.0
        with:
          python-version: "3.12"
      - run: python -m pip install -r requirements.txt
      - run: python -m pytest
      - uses: actions/upload-artifact@<sha>  # v7.0.2
        if: always()
        with:
          name: mendwright-store
          path: |
            .mendwright/overrides.json
            .mendwright/journal.jsonl
          include-hidden-files: true   # .mendwright is a hidden folder
          if-no-files-found: ignore
          retention-days: 1

  mendwright:
    needs: test
    if: always() && needs.test.result != 'cancelled'
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
      actions: read
    concurrency:
      group: mendwright-fix-${{ github.ref }}
      cancel-in-progress: false
    steps:
      - uses: actions/checkout@<sha>  # v7.0.1
        with:
          persist-credentials: false
      - uses: saadbuilt/mendwright@v0.9.0
        with:
          tests-result: ${{ needs.test.result }}
```

Put the commit SHA of each release in place of `<sha>`, as the comment names it, or the tag itself. `saadbuilt/mendwright@v0.9.0` can be a SHA too, with a `# v0.9.0` comment; Dependabot keeps both kinds of pin current.

- The token that can push lives only in the `mendwright` job, which runs none of your test code, packages or browsers. A test dependency that turns hostile gets the `test` job's read-only token.
- `if: always()` runs the job after a failed test job too. A heal from a failed test isn't confirmed, so it is listed, not written. Rerunning the tests later doesn't confirm it either, and the action never writes it: check it and run `mendwright fix --include-unconfirmed --apply` by hand, or delete it from the store.
- With `--mendwright-strict` the tests fail and their heals are still confirmed, so the run is red and the pull request is ready.
- `actions/upload-artifact` leaves out hidden folders unless `include-hidden-files: true`. Upload only the two files: the lock file and anything else in the folder aren't needed.
- The artifact holds the journal, which can quote your pages (the model's `reasoning`, the text of a popup `dismiss=True` closed), and the store's fingerprints, which hold up to 80 characters of each element's text. Anyone who can read the repository can download it. `retention-days: 1` keeps it no longer than the next job needs.
- With the cache from [Keep heals between runs](#keep-heals-between-runs), put the cache step before `pytest`; the store keeps earlier heals, and so does the pull request. With a fresh store per run, as in [Fail on drift](#fail-on-drift), upload the folder you passed to `--mendwright-store`.

#### Permissions and settings

- The job needs `contents: write`, `pull-requests: write` and `actions: read`, and nothing else.
- Turn on Settings, Actions, General, Workflow permissions, "Allow GitHub Actions to create and approve pull requests". Without it GitHub refuses the pull request, and the action says so. The branch is pushed by then, so you can also open the pull request by hand.
- A pull request opened with the default `GITHUB_TOKEN` starts no workflow run (GitHub's rule against loops), so your CI doesn't run on it by itself. Close and reopen the pull request to start it, or pass a GitHub App token as `token`.

#### The other way: workflow_run

For a test workflow you would rather not change, run the action from a workflow of its own:

```yaml
name: mendwright fix

on:
  workflow_run:
    workflows: [tests]
    types: [completed]

jobs:
  mendwright:
    if: >-
      github.event.workflow_run.conclusion != 'cancelled' &&
      github.event.workflow_run.event != 'pull_request' &&
      github.event.workflow_run.head_repository.full_name == github.repository
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
      actions: read
    concurrency:
      group: mendwright-fix-${{ github.event.workflow_run.head_branch }}
      cancel-in-progress: false
    steps:
      - uses: actions/checkout@<sha>  # v7.0.1
        with:
          ref: ${{ github.event.workflow_run.head_sha }}
          persist-credentials: false
      - uses: saadbuilt/mendwright@v0.9.0
        with:
          tests-result: ${{ github.event.workflow_run.conclusion }}
```

The test workflow still uploads the `mendwright-store` artifact; the action downloads it from the run that triggered this one.
The job's `if:` keeps a fork's commit from being checked out at all, and the action checks the same again.
`ref:` matters: under `workflow_run`, `actions/checkout` checks out the default branch's newest commit, not the one the tests ran on, and the action refuses that checkout.

With a browser matrix, upload the store from one job of it (`if: matrix.browser == 'chromium'`), or give each job its own artifact name and run the action once per artifact, each with its own `artifact` and `branch-prefix`.

#### What the pull request says

The title is `mendwright fix: write back 2 healed selectors`. The description lists each selector written back, how it was found and what confirmed it, and what was left alone and why:

````markdown
mendwright healed these selectors while your tests ran, and each heal was confirmed; a list's selector was found by mendwright and accepted with `mendwright lists accept`. This pull request writes them into your code, so the code says what the store says.

Check each new selector before you merge. A passing test confirms a heal, but a test can pass for the wrong reason, and anything that can write your test job's files can write the store.

### mendwright fix

#### Selectors written back (2)

| Key | Was | Now | Found by | Confirmed by | Where |
| --- | --- | --- | --- | --- | --- |
| `accept_terms` (checkbox) | `#accept-terms` | `input[name="legal-consent"]` | fingerprint (no model) | passing test `tests/test_checkout.py::test_signup` | `tests/test_checkout.py:3` |
| `search` (textbox) | `#search-box` | `//input[@name='q']` | model: `AnthropicProvider`, `claude-x` | `expect=` on an action | `tests/test_search.py:8` |

#### Left alone (1)

- `email`: its heal was never confirmed.

From run [#11893412207](https://github.com/saadbuilt/shop/actions/runs/11893412207) on `main` at `3f2a1c9`, with mendwright 0.9.0. Each run rewrites the branch `mendwright/fix/main`. Don't push to it: the action stops updating a branch that has commits it did not make. Comment here instead of editing this description, which the action rewrites.
````

Under each item of "Left alone", a Why section quotes the line `mendwright fix` prints. Every key, selector, path and name from the store, the journal or your files is in a code span or a code block, with control and bidirectional characters written out as `\uXXXX`, so nothing in them can mention someone, link an issue or read in another order than it is written. The job summary shows them the same way.
The description ends with a hidden marker that holds what the action needs to know on the next run: a hash of what it writes, the run and commit, and the keys.
The commit has the same title and one line, `From <run>, tests on <commit>.`, with no key and no selector, and its author and committer are `commit-author-name` and `commit-author-email`, whatever `GIT_AUTHOR_*` and `GIT_COMMITTER_*` variables or `author.*` settings your job has.

The job summary starts with what the action did (`Opened #12: ...`, `#12 is up to date.`, `Skipped: ...`) and shows the same tables.

#### When it does nothing

| Result | When |
|---|---|
| `created`, `updated` | It opened a pull request, or pushed new changes to the open one or rewrote its description. |
| `unchanged` | The open pull request already has these changes. With the same changes it pushes nothing, and a new run id alone changes nothing. |
| `closed` | No change is needed any more, so it closed the open pull request, with a comment that says why for each key. It closes one only after a run whose tests passed, when the store accounts for every key in it, and only with `close-stale: true`. |
| `nothing` | There was no artifact, no store, or nothing to write and no open pull request. |
| `kept-closed` | Someone closed a pull request with exactly these changes without merging it, so it was not opened again. Reopen it to bring it back; different changes open a new one. |
| `blocked` | The branch has a commit the action did not make, such as one from GitHub's "Update branch" button. Merge or close the pull request, or delete the branch, and the next run starts it again. |
| `superseded` | A newer run already updated the pull request, so an older one left it as it is. |
| `dry-run` | `dry-run: true`: it ran `mendwright fix` without `--apply` and wrote the job summary. |
| `skipped` | The run is not one it trusts (see the next table). |

#### Which runs it trusts

The store and journal decide which strings go into your code, so the action takes them only from runs it trusts, and only from the run it is part of or the run that triggered it:

| Event | What the action does |
|---|---|
| `push`, `schedule`, `workflow_dispatch` on a branch | Opens or updates the pull request into that branch, from that commit |
| the same, on a tag | Skips |
| `workflow_run` started by `push`, `schedule` or `workflow_dispatch` in the same repository | Opens or updates the pull request into the run's `head_branch`, from its `head_sha`, with the artifact of that run |
| `workflow_run` started by anything else, such as a pull request or a fork | Skips, and downloads nothing |
| `pull_request` | Skips, or does a dry run with `dry-run: true` |
| `pull_request_target` | Refuses, and the job fails |
| anything else | Skips |

`pull_request_target` gives a job a write token and your secrets while it can see a fork's changes, so the action never runs there.
Then, before it changes anything, it checks that the checkout is the commit the tests ran on and has no changes to tracked files, that the base branch is the default branch (unless `only-default-branch: false`), and that the base is not one of its own branches.
On a public repository it adds a notice: the branch and the pull request are public too, and a healed selector can quote text from your pages.

#### Inputs and outputs

| Input | Default | Meaning |
|---|---|---|
| `artifact` | `mendwright-store` | The artifact that holds the store, from this run or, under `workflow_run`, from the run that triggered it. Empty: read the store from the workspace. |
| `store` | `overrides.json` | The store's path inside the artifact, tried as given and then under `.mendwright/`. With `artifact` empty, a path relative to `working-directory`, tried the same way. No `..`. |
| `working-directory` | `.` | Where your code is checked out; `mendwright fix` runs here. Inside the workspace and the git checkout, and not inside `.github`. |
| `paths` | `.` | One `--path` per line, each inside `working-directory`. |
| `globs` | empty | One `--glob` per line. |
| `destructive-keys` | empty | One key per line. List the keys you pass to `mendwright.open(destructive_keys=...)`, which the store doesn't save. |
| `only-default-branch` | `true` | Open pull requests only into the repository's default branch. |
| `branch-prefix` | `mendwright/fix/` | Ends with `/`; an empty prefix, or one without the `/`, stops the job. Two uses of the action on one base branch need different prefixes. |
| `dry-run` | `false` | Run `mendwright fix` without `--apply` and write the job summary. No commit, no push, no API call that writes. Allowed on `pull_request`. |
| `draft` | `false` | Open the pull request as a draft. |
| `labels` | empty | Comma-separated, at most 10, added when the pull request is opened. |
| `close-stale` | `true` | Close the open pull request when its changes are no longer needed. |
| `tests-result` | empty | Pass `${{ needs.test.result }}` (or `${{ github.event.workflow_run.conclusion }}`). Shown in the summary; `close-stale` needs `success`. |
| `token` | `${{ github.token }}` | The token for the API and the push. |
| `commit-author-name` | `github-actions[bot]` | The author of its commits. |
| `commit-author-email` | `41898282+github-actions[bot]@users.noreply.github.com` | Also how the action knows its own commits. |
| `python-version` | `3.12` | Passed to `actions/setup-python`. Empty: the runner's `python3` or `python`, which must be 3.10 to 3.14. |
| `webhook-url` | empty | A webhook that gets a summary of the run, such as a Slack incoming webhook (see [Alerts](#alerts)). Pass a secret, such as `${{ secrets.SLACK_WEBHOOK_URL }}`. Empty: no alert. |
| `webhook-format` | `slack` | `slack`, or `json` for any other receiver. |
| `notify-when` | `changes` | `changes`, `attention` or `always`, as `mendwright notify --when`. |
| `notify-detail` | `keys` | `keys` names the keys in the alert; `counts` names none. |
| `journal-run` | empty | The run id of the journal events to summarize. Empty: `gh-<run id>` of the run that tested the code, its newest attempt. Pass it only when your test job sets its own `MENDWRIGHT_RUN_ID`. |

Boolean inputs take `true` or `false` only. `webhook-format`, `notify-when`, `notify-detail` and `journal-run` are checked before anything else, as the boolean inputs are.

| Output | Value |
|---|---|
| `result` | One of the results above, or empty when the action refused the run |
| `pull-request-number` | The pull request it created, updated, left as it is, kept closed, blocked on, found newer, or closed |
| `pull-request-url` | That pull request's address |
| `written` | How many keys were written back (in a dry run, would be) |
| `left-alone` | How many items are under "Left alone" |
| `notified` | `true` when the webhook took the summary, `false` when it did not, empty without `webhook-url` |

#### Limits

- One store per use of the action. Stores of several jobs are not merged.
- On a public repository the artifact, the branch, the pull request and the job summary are public. They hold healed selectors, which can quote page text, and test names.
- No git hook runs in the action's commit or push: a hook is code from your repository, in a job that holds a write token. Your CI checks the pull request instead.
- The action installs one lxml wheel into a virtual environment of its own, from `action/requirements.txt`, where its version and the hash of every file of that release are pinned. It builds nothing and runs mendwright from its own tag.
- Keep the action and the mendwright your tests install on the same version. Dependabot's `pip` and `github-actions` updates do that. The action reads stores and journals of older versions; a canary from before 0.9.0 shows as "a check before mendwright 0.9.0".
- GitHub Enterprise Server is not tested.
- What review still has to catch: a store is only as trustworthy as your test job. Anything that can write the test job's files can name a string literal in your code and propose any XPath that compiles in its place. `mendwright fix` refuses values mendwright could not have saved, but an XPath such as `//cdn.example/x.js` is also a protocol-relative URL. Read the pull request as you would read a change from a contributor.

## Checking for drift before a run

`mendwright check` opens the page where each key in the store was last found, in a headless Playwright browser, on a site you name, and looks at the key's saved selectors there the way a run does.
It never clicks, types or presses anything, saves nothing and asks no model.
Each key gets one result: it still resolves, it resolves only through a saved heal, it resolves to an element of another role, a run would heal it from memory, a run would need a model, a run would refuse it, or it couldn't be checked, with the reason.
It exits 1 when a key broke, so a CI job can fail before the nightly run starts.

```text
$ mendwright check --base-url https://shop.example
mendwright check: loading 2 pages from https://shop.example with Playwright chromium (headless).
mendwright check: 10 keys on 4 pages, Playwright chromium (headless), 2.1 s
Store: C:\work\shop\.mendwright\overrides.json

https://shop.example/account  not checked (2 keys)
  The page went to /login instead, so its keys were not checked. It probably needs a
  sign-in or the steps before it: pass --storage-state FILE, or leave the page out with
  --skip-page '/account'.

https://shop.example/account/sign-out  not checked (1 key)
  The path /account/sign-out reads as an action (sign out), and loading it could do
  that, such as end the session your runs use. Pass --load-page '/account/sign-out' to
  check it anyway.

https://shop.example/Orders/*/  not checked (2 keys)
  The address of /orders/* has a part mendwright doesn't save (a record id, a token, an
  email address or a long name), so mendwright check can't load it. Pass --url
  '/orders/*=https://shop.example/orders/<one that exists>'.

https://shop.example/signup  (HTTP 200, 1.3 s)
  would heal     accept_terms    #accept-terms matches nothing. A run would heal it from
                                 memory to //input[@data-testid='legal-consent']
                                 (fingerprint score 0.57, next best 0.40), which passed
                                 every check of a heal on this page. Nothing was saved.
  ok (heal)      create_account  The selectors before it miss (#create-account: no
                                 match); the heal saved on 2026-10-10,
                                 //button[normalize-space(.)='Create account'], matches
                                 one visible button, and a run uses it. Run mendwright
                                 fix to put it in your code. No passing test confirmed
                                 it, so check that it finds the right element.
  ok             email           #email matches one visible input.
  needs model    newsletter      #news matches nothing, and the fingerprint match for
                                 'newsletter' was not confident: two elements score
                                 alike (0.71 and 0.71), so the fingerprint does not tell
                                 them apart. A run would ask AnthropicProvider, model
                                 claude-x. mendwright check asks no model.
  refused        pay             #pay matches nothing. A run would not heal it: 'pay' is
                                 marked as a destructive control, and mendwright never
                                 auto-heals those, because a validated but wrong
                                 selector here could act on a live account. Fix the
                                 selector in your code or seed file by hand.

2 keys were never found on a page with an address (set_content(), about:blank or a data:
URL) and were left out.
1 ok, 1 ok through a saved heal, 1 would heal from memory, 1 needs a model, 1 refused, 5 not checked.
Exit 1: 3 keys broke.
```

Every page is loaded once, and its keys wait for their elements up to `--timeout-ms` together, not one after another.

### Naming the site

`mendwright check` loads only sites you name. Give one of these:

- `--origin https://shop.example` loads the pages recorded on that origin, from that origin. Repeat it for each site to check. Keys recorded on other sites are left out, and counted in one line.
- `--origin https://shop.example=https://staging.shop.example` loads the pages recorded on the first origin from the second, such as production's pages from staging.
- `--base-url http://127.0.0.1:8000` loads every page from that origin, whatever origin was recorded, for a test app that starts on a new port each run. It also builds the address of a key saved before 0.9.0 from its `lastPage`. When the store has pages on more than one host, it says so on stderr, since those may be different sites.

An origin is `scheme://host[:port]` with `http` or `https`, and nothing after it. One recorded origin can be loaded from one site only: naming it twice with two sites is a usage error.
The store is a file from a cache or a commit, so `mendwright check` never loads a site the store alone points to: a store from somewhere else can't send your browser, with the session of your storage state file, to another host, a local file or a cloud metadata address.
With neither flag it exits 2 and lists the sites the store has pages on.

### Where the addresses come from

From 0.9.0 on, each time a key's element is found, the store saves the page's address as `lastUrl`, next to `lastPage`: scheme, host, port and path, without user info, query or fragment. Parts of the path that look like a record id, a token (a JWT, a reset link's code), an email address, percent-encoded text or a name longer than 64 characters are saved as `*`, so the file holds no link that still works for someone's account.
`https://shop.example/Orders/10482/?tab=2` is saved as `https://shop.example/Orders/*/`.
A healer opened on a Playwright `Frame`, or on a Selenium driver switched into a frame, saves no address, and neither does a `file://`, `about:` or `data:` page.
The site root's address is saved only for a key found on no other page, so a header link found on `/signup` and on the home page is checked on `/signup`.

A page whose address has a `*` can't be loaded as it is. Give it an address that exists with `--url`:

```bash
mendwright check --origin https://shop.example --url '/orders/*=https://shop.example/orders/1001'
```

`--url PAGE=URL` wins over the saved address and over `--base-url`. Its `URL` must be on a site the check loads (with `--origin https://shop.example=https://staging.shop.example`, on staging), and may have a query, which the reports print as `?...`.
A key saved before 0.9.0 has only its path: `--base-url` loads it from the stored `lastPage`, which is lower case. It does so only for a path as mendwright saves one. A `lastPage` that holds anything else, such as user info, a port or a query (`@169.254.169.254/latest/`), is `not checked`, so it can't make the address name another host. A server that tells paths apart by case answers 404, which is reported as such. Run your tests once with 0.9.0 and the address is saved.

Some sites change state on a GET. `mendwright check` doesn't load a page whose path reads as an action, as `dismiss=True` doesn't click a button by its words: one whose path has a word `logout`, `logoff`, `signout`, `signoff`, `unsubscribe`, `optout`, `delete`, `remove`, `cancel`, `confirm`, `verify`, `activate`, `deactivate`, `reset`, `approve`, `accept`, `decline`, `reject`, `revoke`, `disconnect`, `unlink`, `leave`, `purge`, `destroy`, `empty`, `clear` or `close`, or the words `log out`, `log off`, `sign out`, `sign off` or `opt out` in a row. Words are split on `/`, `-`, `_`, `.`, `+` and `~` and compared whole, so `/order-confirmation` and `/blog/clearance-sale` load, and `/account/close-account`, `/cart/empty` and `/account/sign-out` don't. So does `/products/close-up-lens`, which is no action: pass `--load-page '/products/*'` for pages like it. A `--url` value is never refused.

### What each result means

| Result | What it means | What to do |
|---|---|---|
| `ok` | A saved selector matches one visible element of the key's role (a hidden checkbox counts, as in a run). | Nothing. |
| `ok (heal)` | The selectors from your code miss, and a heal saved earlier matches. A run uses it, with no model call. | Run [`mendwright fix`](#mendwright-fix) to put it in your code (for a `get_by_*` call, it shows the healed selector and where the call is, and leaves the call to you). It says when no passing test confirmed the heal. |
| `wrong role` | A selector matches one visible element, but it is another control (a button that became a `div`). A run acts on it all the same: mendwright compares roles only for heals. A changed tag on a key you only read from, such as a `span` that became a `div`, is noted on an `ok`. | Check the selector in your code. |
| `would heal` | The selectors miss, and a run would heal the key from memory, with the [fingerprint tier](#heals-without-a-model), or [follow it into an iframe](#iframes). The heal passed every check of a heal on this page, and nothing was saved. | Tonight's run heals it, and `--mendwright-strict` fails on it. Update the selector in your code. |
| `needs model` | The selectors miss, the fingerprint doesn't find the element with confidence, and a run would ask the model provider it names. | Check that the run has a provider, or fix the selector. |
| `refused` | A run would stop with `AttentionRequired`: a key marked destructive, a page the key was never found on, an unknown role, a `guard=` that blocks, `MENDWRIGHT_DISABLE=1`, or no provider and no confident fingerprint. The reason is the run's own, the one [Troubleshooting](#attentionrequired) lists. A `get_by_*` call that matches several elements or one hidden element is `refused` too: a run hands it to Playwright, whose action fails. | Fix the selector by hand, or what the reason names. |
| `list empty?` | A [list](#lists) that had items matches nothing. mendwright can't tell an empty list from a broken one, and never heals a list. | Check the selector, or the page's state. |
| `needs steps` | The key was found during [`run_steps()`](#steps-in-any-order), and the page was loaded without the steps before it, so its miss may be the page's state. | Check it with `heal.drift()` inside your flow. |
| `optional` | Earlier runs skipped the key when it was missing (`optional=True`, or `heal.interrupt()`). Not counted. | Nothing. |
| `not checked` | The key couldn't be checked, and the reason says why: the page answered HTTP 400 or more, went to another page (a sign-in), showed only a sign-in form, didn't load, reads as an action, or has no address mendwright can load; or the key has no active selector, or none that can run. | Pass what the reason names: `--storage-state`, `--url`, `--load-page`, `--skip-page`. |

A key a run heals from memory still broke: tonight it costs a heal, strict mode fails on it, and the selector in your code is out of date.
The words of a `refused` result come from the run's own decision, made by the same code on a copy of the store, so a key is never reported `would heal` when a run would refuse it.

### Exit codes

| Code | When |
|---|---|
| 0 | No key fails, and every key was checked (or `--allow-unchecked`). |
| 1 | At least one key fails. Wins over 3. |
| 2 | A usage error. Nothing was loaded. |
| 3 | No key fails, but one couldn't be checked, or the store has no keys. `--allow-unchecked` makes that 0. |
| 4 | It couldn't run: no store, a store or seed it can't read, no Playwright, a browser that won't start, a storage state file it can't use (also one Playwright refuses), or an error that stopped it before it could report, such as a browser that crashed. It prints one line, never a key's result. |
| 130 | Interrupted. Every browser is closed first. |

- `--fail-on drift` (the default) fails on `wrong role`, `refused`, `would heal` and `needs model`.
- `--fail-on unhealable` fails only on what a run can't get past alone: `wrong role`, `refused`, and `would heal` while dry run is on (a run in dry run applies no heal).
- `--strict` also fails on `ok (heal)`, as `--mendwright-strict` would fail a test that needs it.

The last line of the report says why: `Exit 1: 3 keys broke.`

### Pages behind a sign-in

A page that answers 401 or 403, goes to another page such as `/login`, or shows only a sign-in form when none of its keys match, is reported as not checked.
Save a Playwright storage state after a sign-in and pass it with `--storage-state FILE`:

```python
context.storage_state(path="check-auth.json")
```

Every page gets a new browser context with that state, so nothing one page sets reaches another. The file is read by Playwright and never written, and its contents are never printed.
Make that sign-in for `mendwright check` alone, with its own state file, not the nightly run's: a site that rotates refresh tokens can end the session in the file once `check` uses it, and with reuse detection the nightly run's session too.

### Cookie banners

`mendwright check` asks whether an element is visible, not whether it is on top, so a banner over an element changes no result.
When a key of a page misses and the [`cookies=` preset](#cookie-banners) finds a banner, the page gets a note. Nothing is clicked.
A banner that hides the whole page until it is answered needs `--cookies reject` (or `accept`): the preset closes it before the keys are looked at, and again when keys miss, for a banner a consent script shows a moment after the page loaded, as a run does after a miss. Each time, `handled interruption 'cookie_banner' before '<check>'` is logged. That is the only click `mendwright check` makes. No Playwright overlay handler is registered, and interruptions registered in your code are unknown to it.

### Iframes, lists, steps and optional keys

- **Iframes.** A selector saved inside an iframe is looked at there. A key whose selector now matches only inside one iframe is `would heal`, by the rules of [Iframes](#iframes), and one a run wouldn't follow is `refused` with the run's reason. A key last found in an iframe is healed there in the preview, as in a run.
- **Lists.** A key used with `heal.locator_all()` whose list had two or more items is looked at as a list: any match is `ok`, with the count then and now, and no match is `list empty?`. It is never `would heal`. A list that never had two items, or one recorded with the fingerprint tier off, is looked at as one element. A `list empty?` key with a [list heal](#lists-that-always-have-items-nonemptytrue) that waits says so: one to check with `mendwright lists`, or one you accepted that waits for `mendwright fix --apply`.
- **`get_by_*` calls.** The key of a [`heal.page.get_by_*()` call](#get_by-locators) is looked at as a run looks the call up: the call itself first, and its saved heals only while it matches nothing. A call that matches several elements, or one hidden element, is `refused`: a run hands it to Playwright as written, without its heals, and Playwright's action fails on it. A call found only inside an iframe is `refused` too, since a run never follows one there. A heal of a call comes from its fingerprint only, so a call is never `needs model`.
- **Steps.** `mendwright check` runs no step. A key found during `run_steps()` that misses is `needs steps`.
- **Optional keys.** `optional=True` and `heal.interrupt()` aren't saved in the store, but a run that skipped a key writes `flow.skip` to `journal.jsonl`, and such a key is `optional` when it misses.

### What it never does

- It never clicks, types, presses, scrolls or acts on the page, but for the `--cookies` preset.
- It saves nothing: the store is opened read-only, no journal line is written, no storage state is written back, and no screenshot, trace or HTML is kept. Downloads are refused.
- It asks no model. A heal it looks for runs on a copy of the store in memory, with a provider that stops before any call, and a configured provider is replaced by one that raises if anything calls it.
- It loads no site you didn't name, nothing but `http` and `https`, no user info or fragment, and a query only from `--url`.

### Options

Sites, exactly one of:

| Option | Meaning |
|---|---|
| `--origin ORIGIN[=ACTUAL]` | Load the pages recorded on `ORIGIN`, from `ACTUAL` when given. Can be given more than once. |
| `--base-url URL` | Load every page from this origin. |

The rest:

| Option | Default | Meaning |
|---|---|---|
| `--store PATH` | `MENDWRIGHT_STORE`, else `./.mendwright/overrides.json` | The store. `journal.jsonl` is read from the same folder. A pytest run with the `mendwright_store` ini key needs this. |
| `--seed PATH` | none | A seed file read under the store. Its roles, texts and destructive flags count. |
| `--destructive-key KEY` | none | The keys your run passes to `open(destructive_keys=...)`, which the store doesn't save. Can be given more than once. |
| `--url PAGE=URL` | none | The address to load for a stored page. |
| `--load-page PATTERN` | none | Load pages matching `PATTERN` even when their path reads as an action. |
| `--key PATTERN`, `--skip-key PATTERN` | all keys | Check only the keys matching the glob, or leave them out. |
| `--page PATTERN`, `--skip-page PATTERN` | all pages | The same for the key's page, a path pattern such as `/checkout/*` (`*` matches across `/`, and `/` alone is the site root). |
| `--browser` | `chromium` | `chromium`, `firefox` or `webkit`. |
| `--headed` | headless | Show the browser. |
| `--viewport WxH` | 1280x720 | The window size, each side 200 to 4000. A responsive page shows other elements at other sizes. |
| `--storage-state FILE` | none | A Playwright storage state for every page. |
| `--cookies {reject,accept}` | none | Close a consent banner with the preset first, and again when keys miss. |
| `--timeout-ms MS` | `MENDWRIGHT_TIMEOUT_MS`, else 10000 | How long each page waits for its keys once it has loaded. |
| `--nav-timeout-ms MS` | 30000 | The limit of each navigation, 1000 or more. |
| `--wait-until` | `load` | `load`, `domcontentloaded` or `networkidle`. |
| `--workers N` | 1 | Pages checked at once, 1 to 8. Each worker runs its own browser. |
| `--fingerprint`, `--no-fingerprint` | as a run | Whether to look for heals from memory. `MENDWRIGHT_DISABLE=1` wins. |
| `--fail-on {drift,unhealable}` | `drift` | See [Exit codes](#exit-codes). |
| `--strict` | off | `ok (heal)` counts as broken. |
| `--allow-unchecked` | off | Keys that couldn't be checked don't make the exit code 3. |
| `--format {text,md,json}` | `text` | `md` is for `$GITHUB_STEP_SUMMARY`; `json` has `"schema": 1`. |
| `--out FILE` | stdout | Write the report to a file and print `Wrote <path>`. |
| `--quiet` | off | List only the keys that aren't `ok`, and the summary. |

It reads `MENDWRIGHT_STORE`, `MENDWRIGHT_TIMEOUT_MS`, `MENDWRIGHT_FINGERPRINT`, `MENDWRIGHT_DISABLE` and `MENDWRIGHT_DRY_RUN` as a run does, and the provider variables only to name the provider a run would ask. Run it with the nightly run's environment.

### Checking from your own test with heal.drift()

`heal.drift()` does the same on the page your script has reached, with no navigation, on every driver mendwright supports: sync and async Playwright, patchright, camoufox, Selenium and your own. Use it for pages behind a sign-in flow or earlier steps, for Selenium's own visibility rules, and for `file://` pages:

```python
def test_checkout_selectors(page, heal):
    log_in_and_fill_cart(heal.page)
    page.goto("https://shop.example/checkout/payment")
    report = heal.drift()
    assert report.ok, report.text()
```

- `heal.drift()` checks the keys found on this page before, and `heal.drift(["email", "pay"])` those keys wherever they were found: a key never found on this page gets the page guard's refusal, as in a run, unless `heal_across_pages=True`. The site root has no page, so there `heal.drift()` checks the keys whose saved address is a root of the same host, whatever its port, for a test app that starts on a new port each run. That address is saved only for a key found on no other page (see [Where the addresses come from](#where-the-addresses-come-from)), so a header link found on `/signup` and on the home page is checked by `heal.drift()` on `/signup`, and on the home page by name.
- `timeout_ms=` (the healer's by default) is one wait for the whole page.
- It returns a `DriftReport` from `mendwright.drift`: `ok` (at least one key, none broken, none unchecked; `False` for a page where no key was found), `broken`, `unchecked`, `failed(fail_on=..., strict=...)`, `text()` and `as_dict()`. Each of its `keys` is a `KeyDrift` with the `verdict`, `reason`, `used` selector, `heal` and the `probes` of each selector.
- It uses the healer's store, provider, fingerprint setting, `destructive_keys`, guard, `heal_across_pages` and dry-run setting. A key registered with `heal.interrupt()` is `optional` when it misses.
- It writes nothing, acts on nothing and asks no model, as `mendwright check` doesn't. Playwright's overlay handlers stay out of it. It logs one INFO line, such as `mendwright: drift on /signup: 6 keys, 4 ok, 1 would heal from memory, 1 refused`.
- A list of keys as one string, keys the store doesn't have, and a negative `timeout_ms` raise `TypeError` or `ValueError` before anything touches the page.

On `mendwright.aio.open()`'s healer it is `await heal.drift()`. It holds the healer's lock, and runs the check in a worker thread as a heal does.
A test that calls `heal.drift()` makes no heal, so it confirms nothing and strict mode never fires on it.

### What it prints

- Key names, the store's selectors, origins and paths (saved masked; a redirect's target cut to its page), HTTP statuses, results and reasons.
- A `would heal` XPath and a run's reason for a refusal can quote page text, as heal log lines do.
- Error texts go through the same masking as `mendwright doctor`, and their addresses are cut to origin and page.
- Never cookie names or values, header values, storage values, page HTML or a query string from the site.

The CI logs of a public repository are public. Use `--format md` into the job summary, or `--quiet`, and check what the store's selectors say before you print them there.

### Limits

- `destructive_keys=` of `open()` and `optional=True` aren't in the store. Pass `--destructive-key` for the first; the journal covers the second once a run skipped the key.
- Each key is checked on its last page only.
- The store knows the selectors your code passed on the last run that saved it. A selector you changed since shows as drift until the next run updates the store.
- Elements that show only after a scroll or a click, such as lazy or virtualized rows, are misses.
- A list key whose list never had two items, or was used with the fingerprint tier off, is looked at as one element. Leave it out with `--skip-key`.
- A store written by Selenium holds only XPath and CSS, which Playwright runs, but with Playwright's visibility rules: Selenium counts `opacity: 0` as hidden and Playwright doesn't. For Selenium's rules, call `heal.drift()` from a Selenium test.
- A process of mendwright 0.8.0 that writes a shared store (pytest-xdist workers on mixed versions) drops `lastUrl`. Until a 0.9.0 run saves it again, those keys are `not checked` unless you pass `--base-url`.
- A healer opened on a Playwright `Frame` saves the frame's path as the key's page, and no address. With `--base-url`, and with `--origin` for a key found on the site root before (the store can't tell it from a key two test processes saved at once, one on the root and one on another page), `mendwright check` loads that path on the site you name: a page that may not exist there, or another one. Leave such keys out with `--skip-key`.
- Playwright dismisses JavaScript dialogs (`alert`, `confirm`, `prompt`) when no handler is set, and `mendwright check` sets none.
- The run's rate limit, a guard that blocks at that moment, and interruptions registered in code are unknown to it.
- What it costs, measured on a Windows laptop against a local server: 50 keys on 10 pages, none broken, took 2.2 to 2.6 s in headless Chromium with `--workers 1`, and 3.5 to 4 s with `--workers 3`, since each worker starts its own browser. Workers pay off on pages that are slow to load, not on fast local ones. A real site adds its own load time to each page, and a page where a key misses waits up to `--timeout-ms` (10 s by default), once for the page. Each worker's browser takes the memory of one browser.

## Logging

mendwright logs to `logging.getLogger("mendwright")`.
Each applied heal prints one line to stderr, logged at WARNING so it shows with no logging setup.
A declined heal logs one WARNING too, or an INFO line in dry run. A handled interruption logs one WARNING line, and a skipped step one INFO line. With a format that shows the level, they look like this:

```text
WARNING mendwright: healed 'accept_terms': #accept-terms -> //label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox'] (Stub, 0.0s)
WARNING mendwright: did not heal 'newsletter': mendwright does not know what kind of element 'newsletter' is (its expected role is unknown), so it cannot check a repaired selector. Pass role= to heal.locator(), for example role='button', or set expectedRole for the key in the seed file.
```

To change the format or send the lines elsewhere, configure logging in your app:

```python
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
```

To silence mendwright, raise its logger's level: `logging.getLogger("mendwright").setLevel(logging.ERROR)`.

`open()` also logs one WARNING when no provider can be configured.
pytest shows the lines in the captured output of a failed test.

## Command line

`mendwright` (or `python -m mendwright`) has seven commands, `demo`, `doctor`, `report`, `fix`, `lists`, `model` (see [Local model](#local-model)) and `check`, plus `--version`.

### mendwright demo

Runs a scripted heal on a stand-in checkout page, offline, and exits 0 when the cookie banner is handled, the checkbox heals and the pay button is refused.

- `--browser [chromium|firefox|webkit]` runs it in a real headless Playwright browser.
- `--live` asks the model from your environment instead of the scripted reply. Only the demo page is sent.
- `--keep` keeps the temporary store and prints its path.
- `--slow` pauses between lines, for recording.

It writes only to a temporary folder. Without `--live` it ignores `MENDWRIGHT_*` settings and needs no key.

### mendwright doctor

Prints versions, which browser libraries are installed (it doesn't import them), the provider `mendwright.open()` would use and why, the store path, and any `MENDWRIGHT_*` settings.
It never prints key values and always exits 0. `--json` prints the same as JSON.

```text
Model provider
  none configured, so only fingerprint healing (no model call) can repair a broken selector.
  Why: mendwright.auto_provider: no provider configured. Set one of these, checked in
  this order: ANTHROPIC_API_KEY and ANTHROPIC_MODEL (Anthropic); OPENAI_API_KEY and
  OPENAI_MODEL (OpenAI-compatible); GEMINI_API_KEY and GEMINI_MODEL (Gemini);
  MENDWRIGHT_GATEWAY and MENDWRIGHT_KEY, plus MENDWRIGHT_AUTH_HEADER if your gateway
  uses another header (mendwright gateway); OLLAMA_MODEL, plus OLLAMA_HOST if Ollama is
  not on http://127.0.0.1:11434 (local Ollama). Or set MENDWRIGHT_PROVIDER to one of:
  anthropic, openai, gemini, gateway, ollama, or pass provider=... to mendwright.open().
  `mendwright demo` works without one (it uses a scripted reply).

Fingerprint healing (no model)
  on: each time a selector matches, mendwright records what the element looks like, and
  when the selector breaks it looks for that element first, before any model call. Set
  MENDWRIGHT_FINGERPRINT=0, or pass fingerprint=False to mendwright.open(), to turn it
  off.

get_by_* locators
  not healed (default): heal.page.get_by_role() and the other get_by_* calls go to
  Playwright as written. Set MENDWRIGHT_HEAL_GET_BY=1, or pass heal_get_by=True to
  mendwright.open(), to heal a call that matches nothing from its fingerprint.

Store (where mendwright.open() saves heals)
  C:\work\shop\.mendwright\overrides.json
  (default, relative to this folder; set MENDWRIGHT_STORE to change it)
  the folder does not exist yet; it is created the first time mendwright.open() records a selector
  C:\work\shop is writable
```

`--json` has a `fingerprint` entry with `enabled` and `source` (`default`, `MENDWRIGHT_FINGERPRINT` or `MENDWRIGHT_DISABLE`).
With `MENDWRIGHT_HEAL_GET_BY=1` the get_by section says `healed (MENDWRIGHT_HEAL_GET_BY=1): a heal.page.get_by_*() call that matches nothing is healed from its fingerprint, never by a model.`, or, with the fingerprint tier off, that such a call is routed but reported, not healed. `--json` has a `get_by` entry with `enabled`, `source` (`default` or `MENDWRIGHT_HEAL_GET_BY`) and `heals`, which is `enabled` with the fingerprint tier on.

### mendwright report

Lists each key in a store with its selectors, and the heal counts from the journal.
`--store PATH` picks the file (default `MENDWRIGHT_STORE`, else `./.mendwright/overrides.json`). `--format` is `text`, `md`, `json` or `html`. In `md`, each key and selector is in a code span, with control and bidirectional characters written out as `\uXXXX`, and an empty one shows as (empty).
`--out FILE` writes the report to a file instead of stdout, creating the folder if it is missing, and prints `Wrote <path>`.

```text
Store: C:\work\shop\.mendwright\overrides.json
2 key(s), 3 selector(s)

accept_terms  (checkbox)
  active      code  #accept-terms  (last success: 2026-10-10 19:07:59 UTC)
  active      heal  //label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox']  (last success: 2026-10-10 19:07:59 UTC)
  heals: 1 requested, 1 applied, 0 rejected, 0 quarantined

pay  (button, destructive, never auto-healed)
  active      code  #pay  (last success: 2026-10-10 19:07:59 UTC)

Heals (journal.jsonl): 1 requested, 1 applied, 0 rejected, 0 quarantined
Last applied heals, newest first:
  2026-10-10 19:07:59 UTC  accept_terms  (ScriptedProvider)
    #accept-terms -> //label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox']
```

A list heal that `heal.locator_all(..., nonempty=True)` proposed shows its status `proposed`, with `list of 3 under 'Shopping cart'; check it with: mendwright lists` under it, and an accepted one `list, accepted DATE; write it with: mendwright fix --apply`, or, when the key's selectors come from a seed file, `list, accepted DATE; its selectors come from a seed file, which mendwright fix doesn't change, so put it there in place of .cart-item`. The counts end with `, N held` when the journal has `heal.held` events. In `--format md`, a line under the table says how many list heals wait. `--format json` gives such a candidate `list`, `proposal` and `accepted`, and the counts a `held`.

`--format html` makes one page you can open in a browser or keep as a CI artifact:

```bash
mendwright report --format html --out mendwright-report.html
```

- It has a summary (keys, selectors, quarantined selectors, heals asked for, applied and confirmed, proposals rejected, interruptions, skipped steps and page guard stops), each key with its selectors, the last 25 heals with their old and new selector, state, provider and latency, the last 50 flow events, and the quarantined selectors. Its flow events don't include `flow.step` yet. An interruption handled by a `run=` function names the function, and one whose function raised is listed as failed and isn't counted as handled.
- A heal shows as confirmed when the test that made it passed (the pytest plugin reports that), or when an `expect=` check or `confirm(True)` said so. A fingerprint heal shows `fingerprint` as its provider.
- The page is self-contained: inline CSS only, no scripts, no fonts, no images, and nothing loaded from the network. It follows the system's light or dark theme and stacks its tables on a narrow screen.
- Every value from the store and journal is escaped, and the output is ASCII, so any console encoding can print it.
- Selectors and reasons can quote your pages, so check who can open the file before you share it.

### mendwright fix

Writes healed selectors back into your code, so the code says what the store says.
It finds each key whose best selector is a heal, looks for the key's old selector as a string literal in your source files, and replaces it with the healed one.
Without `--apply` it prints a unified diff and changes nothing:

```text
$ mendwright fix
mendwright fix: 1 replacement in 1 file would put 1 healed selector into your code. Dry run, nothing was changed.

--- a/tests/test_checkout.py
+++ b/tests/test_checkout.py
@@ -1,4 +1,4 @@
 def test_checkout(heal):
     heal.locator("email", "#email").fill("ada@example.com")
-    heal.locator("accept_terms", "#accept-terms", role="checkbox").check()
+    heal.locator("accept_terms", "//label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox']", role="checkbox").check()

accept_terms  (checkbox)  healed, confirmed
  was  #accept-terms
  now  //label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox']
  tests/test_checkout.py:3

Left alone:
  email: its heal was never confirmed (journal.jsonl has no canary success for it; the pytest plugin or an expect= or confirm(True) check writes one in the run that makes the heal). Check that the selector finds the right element, then add --include-unconfirmed to write it anyway.

Run the same command with --apply to write these changes.

Once your code holds the new selector, mendwright tries it first on the next run, because selectors in your code win over saved heals.
```

| Option | Meaning |
|---|---|
| `--store PATH` | The store, found as `report` finds it. `journal.jsonl` is read from the same folder. |
| `--path PATH [PATH ...]` | Folders or files to search, the current folder by default. Can be given more than once. A file named here is searched whatever its extension. |
| `--glob GLOB [GLOB ...]` | Also search files that match, for example `--glob "*.ts" "*.robot"`. `*.py` is always searched. A glob with `/` in it is matched against the path under the `--path` folder. |
| `--destructive-key KEY [KEY ...]` | Treat these keys as destructive, as `mendwright.open(destructive_keys=...)` does, since the store doesn't save that argument. Can be given more than once. |
| `--include-unconfirmed` | Also write heals that were never confirmed |
| `--apply` | Write the changes |
| `--format FORMAT` | `text` (the default) prints what is shown above; `json` prints one document for scripts; `md` prints Markdown for a job summary or a pull request |
| `--out FILE` | Write the output to FILE instead of stdout, and print only `Wrote <path>`. The folder is created when it is missing. |

Which keys it changes:

- The key's most recently successful active selector is a heal, and that heal matched at least once.
- The heal is confirmed: the journal's last `heal.canary` for it is a success. A plain action that worked writes no canary; the pytest plugin writes one when the test that made the heal passes, and so do `expect=` and `confirm(True)`. Rerunning the tests later doesn't confirm an older heal.
- Never a key marked destructive (in the store, or with `--destructive-key`), a heal inside an iframe, or a key whose selectors came only from a seed file.
- Never a heal mendwright could not have saved: one whose selector is not an XPath lxml can compile, or whose CSS form is not one mendwright builds (a tag name, `#id`, `.class` and `[attribute="value"]`, with at most one ancestor before it). Something else wrote it into the store, so it is listed with the reason.
- Never a key whose old selector is a bare word such as `button`, since other strings in your code (a `role=` value, a dict entry) can be the same word.
- A list heal you accepted with `mendwright lists accept` (see [Lists that always have items](#lists-that-always-have-items-nonemptytrue)), under the same rules as a heal, and only in place of the selectors it replaced. Its header line says `list heal, accepted DATE`, and it shows where the group is and what is not the same. Never when your last run passed other selectors for the key, such as another page's or one you fixed by hand, or when one of the key's selectors matched after the list heal was found: the reason says to change the selector by hand, or to drop the list heal with `mendwright lists reject KEY`. Never a list heal that waits for you to check it, with any option: it is left alone with the command that accepts it. Never an accepted one next to a newer proposal for the same key, until you accept or reject that one.
- Never a key with a list heal saved with `"list": true` by builds of 0.8.0 before its release, which nobody checked: it can be another list that holds the items of an emptied one. For a key that also has a heal of one element (`heal.locator()`, a `heal.page` shortcut or `heal.driver.find_element()` under the same key), the reason gives the selector for each.
- Never a key that `heal.locator_all()` looks up as a list when its heal is of one element, from `heal.locator()`, a `heal.page` shortcut or `heal.driver.find_element()` under the same key: written into the list call, it would make the list that one element. The reason names the healed selector.
- Never a `heal.page.get_by_*()` call, whose key is written as the call: it is listed with its healed selector and the lines that make the call (see [mendwright fix and calls](#mendwright-fix-and-calls)). No string equal to a call's text is rewritten either.
- Never a key from `heal.driver`'s `find_element(By.ID, ...)` or `By.NAME`, or from `find_element(By.CSS_SELECTOR, ...)` when the heal has no CSS form, since those calls can't run the healed XPath. The reason says what to change the call to.
- A key whose newest heal was quarantined, and that no selector has matched since, is listed, so a key that drops out of a pull request says why: `email: its newest heal //input[@name='e-mail'] was quarantined (an action on it failed, a check said it was the wrong element, or the key is marked destructive), and no selector has worked for the key since, so there is nothing to write back.`

Which strings it changes:

- In Python files, only plain string literals whose whole value is the old selector, as the store keeps it (`"xpath=//a"` and `" //a "` both match a stored `//a`). Not an f-string or bytes, not part of an implicit concatenation such as `"a" "b"`, not a comment, not a substring.
- Not the key in `heal.locator(key, selector)`, `heal.locator_all(key, selector)` or `heal.interrupt(key, selector)`, and not a `role=`, `text=`, `intent=`, `action=` or `by=` value, even when it equals the selector.
- Never the selector of `heal.interrupt(key, selector)` or the landmark of `heal.step(name, when)`, or a string of a list passed there, even when it equals the old selector of a healed key: written there, that key's heal would make the interruption act on the key's element whenever it shows. Each is listed, for example `tests/test_checkout.py:7 was not changed: '#cookie-accept' is passed to heal.interrupt(), and mendwright fix never rewrites the selector of an interruption. It equals the old selector of accept_terms, so if the interruption broke too, change it by hand.`
  The same goes for a string that reaches the call through a name in the same file: a constant or a variable (also one another function sets under `global` or `nonlocal`), a class constant or a `self.` attribute, a dict or list it is read from or looped over (also what `d[key] = ...`, `append()`, `update()` and the like, or `dict(key=...)`, put in it), or a parameter of a function you define with `def` (its default, and what the calls to it by name in that file pass, `*args` and `**kwargs` included, also when the function has `css` in its name). The line then says which, such as `is passed to heal.interrupt() through ACCEPT`. A string from another module, passed through `functools.partial` or a `lambda`, returned by a function (a pytest fixture included) or built at run time, such as with `+` or an f-string, is not followed, and is rewritten like any other string: check those lines in the diff.
- A CSS selector in your code gets the heal's CSS form when it has one (see [Selectors](#selectors)), also where only CSS runs, such as `By.CSS_SELECTOR` or `querySelector()`. Without a CSS form, a value passed with a Selenium `By` other than `By.XPATH` (in a call, or a `(By.CSS_SELECTOR, "#x")` pair), or to a function with `css` in its name, is listed with the healed selector to put in by hand.
- In other files (`--glob`), quoted strings on one line. Comments are skipped in C-like languages, and template strings with `${` and strings that span lines are left alone. A string right after `By.cssSelector(`, `querySelector(`, `css:` and the like gets the heal's CSS form when it has one, and is otherwise listed, not changed. In languages where only double quotes make a string (Java, Kotlin, C#, Scala, Swift, Go, Rust, JSON), a selector with a double quote in it is escaped, or listed for a hand edit where the escapes aren't known.
- A file in which the same string belongs to more than one key is left alone whole, and listed, because mendwright can't tell which healed selector goes where.

Each rewritten Python file must still parse, have the same structure, and differ only in the planned strings, or it isn't written.
The original quote style and prefix are kept where they can hold the new selector, and a BOM, a coding comment and the line endings are kept.
A file is written through a temporary file next to it and then replaced, and a file that changed since it was read is not overwritten.
`.git`, `.github`, `.venv`, `venv`, `node_modules`, `.mendwright`, `build`, `dist`, caches and `*.egg-info` folders are skipped, and so are symbolic links, Windows junctions, files over 2 MiB and binary files. A `.github` folder holds your workflows and local actions, so it is skipped even when `--path` names it, leads into it through a link or junction, or names it in another case such as `.GitHub`. The folder you run `mendwright fix` from and the folders above it count only when `--path` names them, so a project checked out in a folder named `.github` is still scanned with the default `--path .`, and so is a `.github` folder you run it from: run it from your project's folder. The action refuses a `working-directory` inside `.github`.

`--format json` prints one JSON document and nothing else on stdout, for scripts such as the [GitHub Action](#open-a-pull-request-with-the-fixes). Errors still go to stderr. Cut down:

```json
{
  "schema": 1,
  "mendwright": "0.9.0",
  "store": "/home/runner/work/shop/shop/.mendwright/overrides.json",
  "journal": {"path": "/home/runner/work/shop/shop/.mendwright/journal.jsonl", "exists": true},
  "applied": false,
  "scanned": 14,
  "plan": "5f0c3a...",
  "written_back": [
    {"key": "accept_terms", "role": "checkbox", "old": ["#accept-terms"],
     "now": ["input[name=\"legal-consent\"]"], "xpath": "//input[@name='legal-consent']",
     "css": "input[name=\"legal-consent\"]", "confirmed": true,
     "found_by": {"tier": "fingerprint", "provider": "fingerprint", "model": null, "at": "2026-10-16T05:01:02+00:00"},
     "confirmed_by": {"by": "test", "test": "tests/test_checkout.py::test_signup", "at": "2026-10-16T05:01:04+00:00"},
     "places": [{"path": "tests/test_checkout.py", "line": 3}]}
  ],
  "replacements": [
    {"path": "tests/test_checkout.py", "line": 3, "keys": ["accept_terms"], "selector": "#accept-terms",
     "new": "input[name=\"legal-consent\"]", "was": "\"#accept-terms\"", "now": "'input[name=\"legal-consent\"]'"}
  ],
  "left_alone": [
    {"code": "unconfirmed", "key": "email", "path": null, "line": null,
     "text": "email: its heal was never confirmed (journal.jsonl has no canary success for it; ...)"}
  ],
  "files": [{"path": "tests/test_checkout.py", "replacements": 1, "written": false}],
  "errors": []
}
```

- `written_back` has one entry per key with at least one replacement: `old` is the key's selectors in your code, `now` what replaces them (the heal's CSS form for a CSS selector when it has one), `places` where.
- `found_by` comes from the latest `heal.applied` for the healed selector: `tier` is `fingerprint`, `model`, `intent` or `frame`, with the `provider` and `model` the journal names. It is `null` when the journal has no such event.
- `confirmed_by` describes the `heal.canary` that confirmed the heal: what confirmed it (`by`, see [journal.jsonl](#journaljsonl)) and, for a passing test, its node id (`test`). Both are `null` for a canary written before 0.9.0, and `confirmed_by` is `null` for a heal that is not confirmed, which only `--include-unconfirmed` writes.
- A list heal you accepted with [`mendwright lists accept`](#mendwright-lists) has `"list": true` and `accepted`, when you accepted it. Its `found_by` is the list fingerprint (`tier` `fingerprint`), at the time of its `heal.held`, else of its proposal, and its `confirmed_by` has `by` `accepted`, at the time of its `heal.accepted`, else `accepted`.
- `left_alone` has one entry per line under "Left alone", in the same order, with the line as `text` and a stable `code`.
- `plan` is a SHA-256 of what is written where: each replacement's path, old selector, new selector and keys. Line numbers and quoting are left out, so an unrelated edit that moves a line keeps it.
- `files` says which files were `written`, which only happens with `--apply`.

| `code` | What was left alone |
|---|---|
| `destructive` | a key marked destructive, in the store or with `--destructive-key` |
| `iframe` | a key healed inside an iframe |
| `not_a_selector` | a key whose heal is not an XPath lxml can compile, or whose CSS form is not one mendwright builds |
| `seed_only` | a key with no selector from your code |
| `list_and_element` | a key with a list heal and a one-element heal |
| `list_heal` | a key whose heal is a list heal saved by a build of 0.8.0 before its release |
| `list_proposed` | a key whose list heal waits for you to check it with `mendwright lists` |
| `list_unnamed` | a key whose accepted list heal doesn't name the selector it replaced |
| `list_other_selector` | a key whose accepted list heal replaced a selector your last run didn't pass |
| `list_matched_since` | a key whose selector matched after its list heal was found |
| `list_key` | a list key whose heal is of one element |
| `bare_word` | a key whose old selector is a bare word |
| `driver_by` | a key from a `By` that can't run the healed XPath |
| `unconfirmed` | a key whose heal was never confirmed |
| `quarantined` | a key whose newest heal was quarantined, or a list heal you rejected |
| `get_by_call` | the key of a `heal.page.get_by_*()` call, which `mendwright fix` never rewrites |
| `ambiguous` | a file with a string that belongs to more than one key |
| `not_python` | a file that couldn't be read as Python |
| `css_only` | a place that passes the selector where only CSS runs |
| `no_escape` | a place that needs escapes mendwright doesn't know |
| `flow_selector` | a place that passes the old selector to `heal.interrupt()` or `heal.step(when=)` |
| `already_in_code` | a key whose healed selector your code already has |
| `not_found` | a key whose old selector was found nowhere |

`--format md` renders the same document as Markdown, as the GitHub Action puts it in the pull request: a table of the selectors written back with how each was found and what confirmed it, then what was left alone. Every key, selector, path and name in it is in a code span or a code block.

It exits 0 when there is nothing to do, after a dry run and after `--apply`, and 1 when there is no store, a `--path` doesn't exist, a file couldn't be written, or `--out` couldn't be written.
With `--apply`, the files are written before the output, which is about them. When the store can't be read, nothing is printed on stdout and no `--out` file is written.
Once your code holds the healed selector, mendwright tries it first on the next run.

### mendwright lists

Lists the list heals that `heal.locator_all(..., nonempty=True)` found and saved for you to check, and accepts or rejects one. mendwright never uses one of them: an accepted one is used once `mendwright fix --apply` wrote it into your code (see [Lists that always have items](#lists-that-always-have-items-nonemptytrue)).

```text
mendwright lists [--store PATH] [--json]
mendwright lists accept KEY ID [--store PATH]
mendwright lists reject KEY [ID] [--store PATH]
```

```text
Store: C:\work\shop\.mendwright\overrides.json
1 list heal waits for you to check it.

cart  (div elements)  id 3f2a9c1d
  found    2026-11-02 10:14:03 UTC on /cart, from the list fingerprint (score 0.86, next best 0.31)
  was      .cart-item: 3 elements under 'Shopping cart', in <section class="cart">
           first items: 'Blue mug$12.00Delete', 'Red mug$13.00Delete'
  now      //div[contains(concat(' ', normalize-space(@class), ' '), ' cart-row ')]
           as CSS: div.cart-row
           3 elements under 'Shopping cart', in <section class="cart">
           they hold 3 of the 3 items recorded
           first items: 'Blue mug$12.00Delete', 'Red mug$13.00Delete'
  accept   mendwright lists accept cart 3f2a9c1d
  reject   mendwright lists reject cart 3f2a9c1d

1 accepted list heal waits for mendwright fix --apply:
  search  //li[contains(concat(' ', normalize-space(@class), ' '), ' hit ')]  (accepted 2026-10-30 08:00:00 UTC)
```

- `--store PATH` picks the file, as `report` finds it. The commands it prints get `--store "PATH"` when the store is not the default one, and a key with spaces or quotes is quoted for your shell.
- A proposal whose heading or container is not the list's has `check` lines that say so. Read them, and look at the page, before you accept.
- An accepted list heal of a key whose selectors come from a seed file is listed under `waits for you to put it in the seed file its key's selectors come from, which mendwright fix doesn't change`, with the selector it replaces.
- A proposal that a mendwright 0.8.0 run wrote over has `no saved evidence`, and can't be accepted until a 0.9.0 run finds it again.
- `--json` gives `{"store", "proposed": [{"key", "id", "xpath", "css", "proposal", "accept", "reject"}], "accepted": [{"key", "xpath", "css", "acceptedAt"}]}`, with the strings as stored.
- `accept KEY ID` takes the proposal with that id, which changes whenever what `mendwright lists` shows changes, so you accept what you looked at. It replaces the key's other list heals, and changes no other row of the file.
- `reject KEY` quarantines the key's list heal (give its id when there are several), and mendwright never proposes it, or another selector for the same elements, again. It also drops one you accepted.
- Every string from the page is shown with its control characters as `?`.

It exits 0 after a listing, even an empty one, and after an accept or a reject, 1 when the store, the key or the id is not there, the id changed or the file can't be written, and 2 for a usage error. There is no `--all`: each list needs a look. `accept` and `reject` write `heal.accepted` and `heal.quarantine` to the journal next to the store.

### mendwright check

Loads the page where each key was last found, on a site you name with `--origin` or `--base-url`, in a headless Playwright browser, and says which keys still resolve, which a run would heal and which it would refuse, without acting on the page, saving anything or asking a model. It exits 1 when a key broke and 3 when one couldn't be checked.
[Checking for drift before a run](#checking-for-drift-before-a-run) has the results, the exit codes and every option.

## Low-level API

`mendwright.open()` is built from public functions you can call yourself, for example to use a seed file or your own resolution loop:

```python
import mendwright
from mendwright import HealCoordinator, load_store, resolve, resolve_with_heal, wrap

store = load_store("selectors.seed.json", ".mendwright/overrides.json")
driver = wrap(page)
coordinator = HealCoordinator(
    store,
    provider=mendwright.auto_provider(),
    driver=driver,                     # turns on the live-page check
    html_provider=driver.outer_html,
    inventory_provider=driver.inventory,
    guard=lambda: True,
)

key = "accept_terms"
box = resolve_with_heal(lambda: resolve(store, page, key, timeout_ms=5000),
                        coordinator, key, store.meta(key), {"url": page.url})
box.check()
coordinator.confirm_pending(key, box.is_checked())
```

- `resolve(store, page, key)` returns the native locator or element, or raises `AttentionRequired`.
- `resolve_with_heal(...)` heals once and retries when the resolve function raises.
- Without `driver=`, `HealCoordinator` checks proposals only against the captured HTML, and the live check happens through the retry.
- These paths skip what the facade adds: the code-first selector order and role learning. A key marked destructive in the seed file, in `load_store(..., destructive_keys=...)` or by an earlier `destructive=True` is still refused, because `store.meta(key)` carries the flag.
- `HealCoordinator` writes no journal and has no rate limit unless you pass `journal=` and `guardrails=`, and runs no fingerprint tier unless you pass `fingerprint=True`.

## Troubleshooting

### AttentionRequired

The first line names the key. The line after `->` says why there was no heal:

| The message says | What to do |
|---|---|
| is marked as a destructive control | Fix the selector by hand. If you marked it with `destructive=True` and the control is safe to heal, pass `destructive=False` |
| no provider configured | Set a key and its model variable, or pass `provider=`. Without one, only fingerprint heals happen. |
| has no fingerprint yet | The key never matched while the fingerprint tier was on, so there is nothing to look for. With no provider, fix the selector by hand or set one up. |
| the fingerprint match for '<key>' was not confident | The reason after it says why (two elements alike, no shared id or text, a score too low, outside the recorded container). With a provider the model is asked next; without one, fix the selector or set one up. |
| has no selector yet, and only a model can find it from intent= | Set up a provider, or pass a selector |
| `<Provider> returned no selector for '<key>': <reason>` | The reason is the provider's own error, such as an HTTP 401 for a bad key, an unknown model or no network. Fix what it names, or try another model. |
| the model says no element on this page fits | The model saw no element for the step on this page. If the step moved or the element was removed, update your script. See [When the model finds no element](#when-the-model-finds-no-element). |
| That looks like a change in the flow, not a renamed element | The key was found on another page before. Update your script if the step moved, or pass `heal_across_pages=True` if only the URL changed. See [Steps that moved to another page](#steps-that-moved-to-another-page). |
| interruptions were handled for '<key>' and '<interruption>' is still on the page | The interruption came back each time it was handled. Check the selector and `action=` you passed to `heal.interrupt()`, or, when the message names its `run=` function (`Check that relogin(), the run= function registered with heal.interrupt('session_expired', ...), really closes it.`), that the function closes it. |
| does not know what kind of element | Pass `role=` |
| was healed to ..., and its inner_text() there is ..., which is not money | A read through a heal returned a value that fails `expect_value=`, so the heal was quarantined. The next lookup heals again without that element. If the value is right, change `expect_value=`. See [Checking what a read returns](#checking-what-a-read-returns). |
| Either the list is empty at this point, or its selector broke | A `heal.locator_all()` list that had items matches nothing, and mendwright never heals a list. If the selector broke, change it in your code, or pass `nonempty=True` where the list always has items, to have the new selector proposed. If the list can be empty there, pass `optional=True` to that call. See [Lists](#lists). |
| Accept it with: mendwright lists accept | `nonempty=True` found the list somewhere else on the page and saved it for you to check. Run `mendwright lists`, look at the page, then accept or reject it, and run `mendwright fix --apply`. See [Lists that always have items](#lists-that-always-have-items-nonemptytrue). |
| You accepted a list heal for it on | The list heal you accepted waits for `mendwright fix --apply` to put it in your code. mendwright never uses it before. |
| mendwright looks for a list only while the fingerprint tier is on | Turn the tier back on (drop `fingerprint=False`, `MENDWRIGHT_FINGERPRINT=0` or `MENDWRIGHT_DISABLE=1`), or fix the selector in your code. |
| its list fingerprint was recorded by mendwright 0.8.0 or before a call with nonempty=True | mendwright doesn't know where the list sat. Fix the selector in your code; the next lookup with `nonempty=True` that matches records it. |
| names a state the recorded list didn't have | The group mendwright found is picked by a word such as `done` or `saved`, so it is most likely the list's items in another state, or another list. If the list can be empty here, remove `nonempty=True`; if the selector broke, change it in your code. |
| no selector the model proposed passed mendwright's local gate | Read the last reason: `not unique (3)`, `role mismatch`, `not uniquely present on the live page` or `previously quarantined`. Try another model, or fix the selector by hand. |
| the model returned no usable selector | Try a different model, or fix the selector by hand |
| is inside an iframe ... mendwright did not look for it there | The reason says why (a destructive key, several iframes or elements, an iframe inside an iframe, or one nothing tells apart). Open mendwright on the frame (see [Iframes](#iframes)) |
| does not follow a get_by_* call into an iframe | The call matches only inside an iframe. Open mendwright on that frame with `heal_get_by=True`: `mendwright.open(page.frame(name="..."), heal_get_by=True)`. See [get_by locators](#get_by-locators). |
| heals a get_by_* call only from the fingerprint it records | The call never matched one element in this store while the fingerprint tier was on. Check its arguments against the page: a typo in the name, or a role such as `"btn"` that no element has. |
| asks no model to heal a get_by_* call | The fingerprint found no confident match for the call's element, or its pick failed a check. Change the call by hand to the element's new role or words. |
| Playwright does not give it the ARIA role | The element the fingerprint found no longer has the call's role, or level. Change the call by hand. |
| rate/spend cap was reached | Wait for the hourly window, or raise `rate_per_key_per_hour` and `rate_global_per_hour` |
| dry-run mode is on | Unset `MENDWRIGHT_DRY_RUN`, or drop `dry_run=True` or `--mendwright-dry-run` |
| turned off by MENDWRIGHT_DISABLE=1 | Unset it, or drop `--mendwright-disable` |
| a transaction guard is currently blocking heals | Your `guard=` returned False. Run the step again once it allows heals. |
| there is no stored selector | Pass a selector on the first call for that key, or `intent=` and `role=` |

A heal that picked the wrong element is quarantined when the action fails or `expect=` is false.
To remove it by hand, delete the candidate from `overrides.json`. `mendwright report` shows what is stored.

### InvalidSelector

`InvalidSelector` is a `ValueError`. It is raised at once, without waiting out the timeout or asking a model, for an empty selector, a broken XPath or a Playwright selector on Selenium, which are caught before any browser call, and for CSS the browser rejects, which is caught on the first probe.
It isn't an `AttentionRequired`, so no model is asked.
One that says `is a get_by_* call, not a selector` got a string written as a `get_by_*` call, such as `'get_by_role("button", name="Go")'`, as a selector of `heal.locator()`, `heal.locator_all()`, `heal.interrupt()` or `heal.step(when=...)`. Call `heal.page.get_by_role(...)` on `mendwright.open(page, heal_get_by=True)` instead, or pass an XPath, CSS or Playwright selector (see [get_by locators](#get_by-locators)).

### TypeError

| The message says | What to do |
|---|---|
| is an async Playwright object | Use `heal = await mendwright.aio.open(page)` |
| is a sync page or driver, and mendwright.aio.open() takes an async Playwright Page | Use `mendwright.open(page)` |
| points at elements, not at a page | Pass `locator.page`, or a `Frame` from `page.frames` |
| where a selector was expected | Pass a selector string, not a Locator object |
| could not detect a browser driver | Pass a Playwright page or Selenium driver, or `register_driver` your own |
| on Selenium does not support | Selenium ignores only `timeout=`, `force=`, `no_wait_after=` and `delay=`. Remove the other Playwright options. |

### The GitHub Action

Each message starts with `mendwright:`. A notice or a warning leaves the job green; an error fails it, and the job summary says `Stopped:` and the message.

| The message says | What to do |
|---|---|
| this workflow runs on pull_request_target | Run the action in a job that needs your test job on `push`, `schedule` or `workflow_dispatch`, as in [Open a pull request with the fixes](#open-a-pull-request-with-the-fixes). |
| skipped on pull_request | Nothing, or set `dry-run: true` to see what it would write. |
| the run that made this store was triggered by | The store came from a pull request or a fork. The action only uses stores from `push`, `schedule` and `workflow_dispatch` runs in your repository. |
| only-default-branch is true | Set `only-default-branch: false` to open pull requests into other branches too. |
| the checkout is at ..., but the tests ran on | Under `workflow_run`, pass `ref: ${{ github.event.workflow_run.head_sha }}` to `actions/checkout`. |
| the checkout has changes to tracked files | Run the action right after `actions/checkout`, so the pull request holds only mendwright's changes. |
| is not inside a git checkout | Run `actions/checkout` first, and check `working-directory`. |
| is inside .github/, which mendwright fix never changes | Set `working-directory` to the folder that holds your tests. |
| the action needs Python 3.10 to 3.14 | Set `python-version`, or leave it at its default. |
| has no artifact named | Upload the store with `actions/upload-artifact` and `include-hidden-files: true`, under the name the `artifact` input gives. |
| has no overrides.json at its top or in .mendwright/ | Upload the folder that holds the store, or set `store`. |
| is a link or points outside it | Upload the files themselves, not links to them. |
| over the action's limit | Move the old journal aside, or stop carrying it from run to run. |
| there is no journal.jsonl next to the store | Upload `journal.jsonl` with `overrides.json`: without it no heal counts as confirmed. |
| mendwright fix could not finish | The tracked files it wrote were put back, and the message names any file git does not track, which keeps the change. Fix what the errors above say. |
| has commits that this action did not make | Merge or close the pull request, or delete the branch, and the next run starts it again. |
| changed while this run worked on it | Nothing: another run pushed meanwhile, and the next run picks it up. |
| git push to ... was refused | Give the job `contents: write`, or pass a token that can push. A ruleset that protects `mendwright/**` or asks for signed commits also refuses it. |
| Actions may not create pull requests in this repository | Turn on "Allow GitHub Actions to create and approve pull requests" in Settings, Actions, General, or pass a GitHub App token as `token`. |
| GitHub refused to open the pull request (HTTP 403 | Give the job `pull-requests: write`. |
| had exactly these changes and was closed without merging | Reopen the pull request to bring it back. |
| is still open: | The job summary says why the action could not tell whether the pull request is still needed. Close it by hand when it isn't. |
| rate limit stopped the action | Run the workflow again later. |
| a redirect, which the action does not follow | The repository was probably renamed or moved. Run the workflow from its new place. |

### Other problems

- A missing element waits `timeout_ms` (10 seconds by default) before the heal starts. Lower `MENDWRIGHT_TIMEOUT_MS` while you write a script.
- `mendwright doctor` shows which provider and store a run will use.
- A corrupt or unreadable `overrides.json` raises when the store loads. mendwright never treats it as empty and overwrites it. Fix the file or move it aside.
- Healed selectors can contain page text, such as a label. Treat `overrides.json` like any test fixture and review it before you commit it.
- What leaves your machine, and what redaction misses, is in [SECURITY.md](../SECURITY.md).
