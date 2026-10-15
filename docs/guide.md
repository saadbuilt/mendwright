# mendwright guide

The [README](../README.md) gets you to a first heal.
This page covers the rest: settings, providers, the pytest plugin, async and Selenium details, the files mendwright writes, CI, the command line and troubleshooting.

Contents:

- [Configuration](#configuration)
- [Heals without a model](#heals-without-a-model)
- [No changes per step](#no-changes-per-step)
- [Describing an element with intent=](#describing-an-element-with-intent)
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

The two rates are shared by every Healer in one process that uses the same store file and the same two rate values.
`GatewayProvider` gets no client-side limit, because the gateway meters on its side.

### Healer.locator()

```python
heal.locator(key, selector=None, *, role=None, text=None, destructive=None,
             timeout_ms=None, poll_ms=200, optional=False, intent=None)
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

It returns an `ActionProxy` once the element is found, healed or not, or a proxy with `.present` False for a step that was skipped.
It raises `AttentionRequired` when the element can't be found or healed, `InvalidSelector` or `TypeError` for a selector that can never work (or an `intent=` that is not a non-empty string), and `ValueError` for an unknown role or for `intent=` with no selector and no role.

Other `Healer` members:

- `heal.interrupt(key, selector, *, action="click", args=(), role=None)`: registers a step that can show up at any point. See [Interruptions](#interruptions).
- `heal.handled_interruptions`: the interruptions this Healer handled, as `InterruptionEvent(key, before)`. With `dismiss=True`, the popups it dismissed are listed too, under the key `<popup>`, with `popup` (how the log line names it), `how` (`"escape"`, `"close"` or `"decline"`) and `label` (the button's) set (see [Popups mendwright wasn't told about](#popups-mendwright-wasnt-told-about)).
- `heal.present(key)`: True if a stored selector matches one element at the moment of the call. It doesn't wait or heal.
- `heal.page` and `heal.driver`: the page or driver to use in place of yours, so steps heal without changing them. See [No changes per step](#no-changes-per-step).
- `heal.heals`: the heals this Healer applied, as `HealEvent(key, old, new, confirmed, source)`. `source` is `"fingerprint"` (no model call), `"model"`, `"intent"` for a key that had no selector and was found from its `intent=`, or `"frame"` for a key whose own selector matched only inside an iframe (see [Iframes](#iframes)).
- `heal.pending()`: `{key: xpath}` for heals that aren't confirmed yet.
- `heal.confirm_all(True)` or `heal.confirm_all(False)`: confirm or quarantine every pending heal.

### Actions and confirmation

The proxy runs these actions through the driver: `click`, `dblclick`, `fill`, `type`, `press`, `press_sequentially`, `check`, `uncheck`, `set_checked`, `select_option`, `hover`, `tap`, `clear`, `focus`, `set_input_files`, `drag_to`, `send_keys` and `submit`.
Keyword arguments pass through, and the return value is what the native call returned.
Anything else, such as `inner_text()`, `get_attribute()` or `is_checked()`, goes to the native object unchanged.
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
- you call `proxy.confirm(True)` or `heal.confirm_all(True)`
- the pytest plugin sees the test pass

It is quarantined when the action raises in the browser, when `expect=` returns something falsy or raises, or when you call `confirm(False)`.
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
| `MENDWRIGHT_PROVIDER` | `anthropic`, `openai`, `gemini`, `gateway`, `local` or `ollama`: use that provider instead of detecting one |
| `MENDWRIGHT_GATEWAY` | Gateway URL, which selects `GatewayProvider` |
| `MENDWRIGHT_LOCAL_MODEL` | The Ollama name of mendwright's local selector model, which selects `LocalModelProvider` |
| `MENDWRIGHT_LOCAL_MIN_CONFIDENCE` | The least probability a pick of the local model needs to be used, from 0 to 1 (default 0.8, 0 turns the check off) |
| `MENDWRIGHT_KEY` | The client key the gateway checks |
| `MENDWRIGHT_AUTH_HEADER` | The header that carries it, `X-Heal-Key` by default |
| `MENDWRIGHT_REQUIRE_ENGINES` | Only for this repo's browser tests: comma-separated engine ids that must start, so CI fails instead of skipping |

Flags take `1`, `true`, `yes` or `on`, and `0`, `false`, `no`, `off` or empty.
Any other value makes `open()` raise `ValueError` (for `MENDWRIGHT_DRY_RUN`, only when `dry_run=` isn't passed).

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
- The reads heal too: `is_checked`, `text_content`, `inner_text`, `inner_html`, `get_attribute`, `input_value`, `is_enabled`, `is_disabled` and `is_editable` on a locator, and `is_checked`, `text_content`, `inner_text` and `get_attribute` on the page.
- Reads, `set_input_files` and `focus` work on hidden elements in Playwright. For these, a selector that matches a hidden element, or several, goes to the real page as written when no saved heal finds the element. Only a selector that matches nothing is healed. The actions that need one visible element heal any selector that doesn't match one.
- `is_visible(selector)` answers at once and never heals. For a selector mendwright has saved, it checks the saved selectors, heals included, without scrolling or writing the store.
- These are not healed and go to the real page: a locator built with options (`has_text=`, `has=`), a chained locator such as `page.locator("form").locator("button")`, `.first` and `.nth()`, `get_by_role()` and the other `get_by_*` locators, `wait_for_selector()`, `query_selector()`, `expect(page.locator(...))`, and every other page method (`goto`, `evaluate`, `keyboard`, `frames`, and so on).
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
- These are not healed: the other `By` values (`LINK_TEXT`, `CLASS_NAME`, `TAG_NAME`, and so on), `find_elements()`, and `find_element()` or `find_elements()` on a returned element. Their elements come back as `HealingElement`s too. Every other driver method is the real driver's.
- A `HealingElement`'s `click()`, `send_keys()`, `clear()` and `submit()` handle a registered [interruption](#interruptions) that covers the element first, as a proxy action does, and try a click that something covered once more after handling one. Otherwise they raise Selenium's own error. With no interruption registered and `dismiss` off, they make no browser call besides the element's own; with `dismiss=True`, one more, to look for a popup. `ActionChains` actions, and the elements that `execute_script()` or `switch_to.active_element` return, are Selenium's own and get no such check.
- A `HealingElement` passes `isinstance(el, WebElement)`, is equal to the element it wraps (and hashes the same), and works with `driver.execute_script(..., el)`, `ActionChains` and `expected_conditions`. One around a `FramedElement` needs `in_frame()` for those, as before (see [Selenium](#selenium-1)). Everything else is the element's own, and `el.native` is the element.
- A missing element waits `timeout_ms` before it is healed, where Selenium raises at once unless you set an implicit wait. To check that an element is absent, use `driver.find_elements(...)` or `driver.native`.
- When an action fails in the browser on an element that `find_element()` looked up through `heal.locator()`, a heal made for it is quarantined, as for a proxy action. The elements of the other `By` values and of `find_elements()` were never healed, so their actions confirm and quarantine nothing. A `StaleElementReferenceException` quarantines nothing either: the page re-rendered the element after it was found, which says nothing about the heal, and a new `find_element()` finds it again. A heal whose actions went through is confirmed by the pytest plugin when the test passes, and `heal.confirm_all(False)` quarantines it.

On a Selenium healer, `heal.page` is still the WebDriver.

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

The dry-run flag also reaches Healers that tests open themselves, unless that code passes `dry_run=`. The disable flag reaches all of them, and no argument overrides it.

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

### Strict mode

`--mendwright-strict` fails a test that passed only because a selector was healed. The heal is still saved and confirmed:

```text
______________________________ test_accept_terms ______________________________
mendwright --mendwright-strict: this test passed only because 1 selector was healed. The heal is saved, but the selector in your code no longer matches the page:
  accept_terms: //input[@id='terms'] -> //input[@id='terms-v2']
Update the selector in your code to the new one, or run without --mendwright-strict to let heals pass.
```

Strict mode only fires on the run that makes the heal.
It fails tests that needed a heal, and nothing else: a handled interruption (`flow.interrupt`) or a dismissed popup (`flow.dismiss`) doesn't fail a test.
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
- `heal.interrupt()`, `cookies=`, `dismiss=True`, `optional=True` and the page guard work as on the sync Healer. `interrupt()` is a plain call; the action is awaited when the interruption is handled, and Playwright's overlay handler for it is registered by a task on the event loop, in place a moment later. `interrupt(key, None)` removes it the same way, and the changes to one page's handlers run one at a time, in the order of the calls. `proxy.present` raises `RuntimeError` until the proxy has been awaited.
- Actions awaited together, for example with `asyncio.gather()`, look for interruptions one at a time, so two of them never click the same banner.

A heal runs the same sync coordinator in a worker thread through `asyncio.to_thread`, so the model call doesn't block the event loop.
Every page call the heal makes is handed back to the event loop and waited for, up to `mendwright.aio.SyncBridge.timeout` (30 seconds).
Calls on one `AsyncHealer` take turns. `asyncio.gather()` over several of its locators is safe, but they resolve one after another.
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
| A step that can show up anywhere: a cookie banner, a newsletter modal, an "are you still there?" dialog | Handles it when it is in the way, if you registered it with `heal.interrupt()` or `cookies=` |
| A popup nobody registered: a newsletter or discount modal, a survey, a chat invite | Dismisses it when it covers the element of an action, if you pass `dismiss=True`: Escape, then its close button, then a button such as No thanks |
| A step that may not be there | Skips it, if you pass `optional=True` |
| A step that moved to another page | Declines to heal the key on a page where it was never found, before any model call |
| A step that moved away while the address stayed the same | Leaves it to the model, which is told it may answer that no element fits. The error then gives the model's reason. |

Steps in a new order across pages, and a new required step such as a field you must fill before you can continue, still need a change to your script.

### Interruptions

```python
heal = mendwright.open(page)
heal.interrupt("cookies", "#cookie-accept")
heal.interrupt("newsletter", "//div[@id='newsletter']//button[@aria-label='Close']")
heal.interrupt("still_there", "#idle-dialog", action="press", args=("Escape",))

heal.locator("email", "#email").fill("ada@example.com")
```

`heal.interrupt(key, selector, *, action="click", args=(), role=None)` registers a step that can show up at any point.
`selector` finds the element to act on: one selector, or a list tried in order. `action` and `args` say what to do with it, with the same verbs as a proxy action. With `role=`, mendwright acts only on an element of that role (on drivers that can read elements).
Use a selector that matches one element. An XPath that matches several is narrowed to the first visible match, but a CSS or Playwright selector that matches several, such as `.cookie-accept` matching a hidden mobile copy and the visible button, is skipped.

mendwright looks for registered interruptions only when a step needs help:

- when a `locator()` misses, before any heal and before any model call
- before an action, when the page shows one of them (a quick check for each registered interruption, only when you registered any)
- when an action fails because something covered the element: Playwright's "intercepts pointer events" or "element is not visible", Selenium's `ElementClickInterceptedException` or `ElementNotInteractableException`

On Selenium, the elements `heal.driver` returns look for registered interruptions in the same way before `click()`, `send_keys()`, `clear()` and `submit()`, whatever `By` found them (see [No changes per step](#no-changes-per-step)).

- On Selenium, the check before an action, of `heal.locator()` and of `heal.driver`'s elements, handles an interruption only when something covers the element where a click on it lands, or when that can't be told (for an element outside the viewport, or inside an iframe). So a step of your own on the interruption itself goes ahead and does not find it gone: the banner's Reject or Manage preferences button, or the email field of a newsletter form. If the action fails because something covered the element after all, the interruptions are handled and it is tried once more, as above.
- `ActionChains` actions and the elements that `execute_script()` or `switch_to.active_element` return are Selenium's own, so nothing is checked before their actions.
- After the script calls `switch_to.frame()` itself, mendwright looks for interruptions in that frame only, and a banner on the page around the frame is out of reach. When a click there is intercepted, the WARNING says so. Find the element from the page without switching instead: `heal.driver` follows a selector into an iframe one level down (see [Iframes](#iframes)).

On a Playwright `Page`, from Playwright 1.42, `heal.interrupt()` and `cookies=` also register each interruption with `page.add_locator_handler()`. Playwright then checks for it before each locator or selector action on the page (`click`, `fill`, `check`, `hover` and so on), and again each time an action that waits (for a disabled button, say) tries once more, and hands over to mendwright when the page shows it. So it is handled before the actions mendwright does not route as well: `get_by_role()` and the other `get_by_*` locators, chained locators, `.first` and `.nth()`, and the page's own selector methods, on `heal.page` or on the page itself. A banner that opens while an action waits is handled at once, instead of after the action's timeout.

- Playwright runs no handler before `page.mouse` and `page.keyboard` actions, `dispatch_event()`, or an action with `force=True`, so those get no such help.
- Such an action has no key, so the WARNING line and the journal event say `before '<page action>'`. One that happens during a step of mendwright, such as the scroll before a click, is logged before that step and counts toward its 3.
- An interruption that is still on the page after mendwright handled it 3 times in a row this way is left alone, with one WARNING, until mendwright sees it gone (a step that mendwright routes looks for it) or the page navigates or loads: `goto()`, a link, `set_content()`, or a history change in a single-page app.
- A page keeps one handler per key, however many healers you open on it: one that registers the same selectors joins the handler there, which acts for the newest of them that is still alive. Registering a key with other selectors replaces it. From Playwright 1.44 the old one is removed from the page, unless the handler of another key watches the same selectors, because Playwright would remove both. Before 1.44, and in that case, it stays and does nothing.
- `heal.interrupt(key, None)` removes an interruption from the healer, `cookie_banner` included, and its handler from the page (Playwright 1.44 and later) once no other healer of the page has registered it. A healer you stop using is not freed at once, because it refers to itself (through `heal.page`, for one): Python frees it when its garbage collector next runs, and until then its handler still acts. When the page outlives the healer, such as a page shared between tests, call `heal.interrupt(key, None)` for each key once you are done with it.
- Playwright runs the check at least twice per action, as it finds the element and as it acts, and again while it waits or retries. So each registered interruption costs at least two queries of the page per action. With `cookies=`, on a page of 2,000 elements in Chromium, that added 15 to 50 ms to each action.
- A selector the browser cannot parse would fail every action on the page in that check, with an error that names the interruption's selector. `heal.interrupt()` checks XPath without a browser but leaves CSS to the browser (`'button:contains("Close")'` passes it), so mendwright tries each CSS or Playwright selector once on the page when it registers the handler. One the browser rejects is left out, with a WARNING that names it.
- Playwright also runs the check before an auto-waiting assertion, `wait_for_selector()`, `Locator.wait_for()`, an element screenshot and `aria_snapshot()`, on the page itself too. So `expect(page.locator("#cookie-banner")).to_be_visible()` and `page.wait_for_selector("#cookie-banner")` fail once the banner is registered: mendwright closes it first, and the wait runs out. A step of your own that clicks the banner through a locator mendwright does not route, such as `page.get_by_role("button", name="Accept all").click()`, now finds it gone and waits out its timeout. Drop such a step, or write it as `heal.locator(key, selector)` with the interruption's key, which is skipped when the element isn't there.
- A `Frame` has no `add_locator_handler()`, so a healer opened on one, and Playwright before 1.42, keep the checks above only.

When one is there and visible, mendwright performs its action, waits up to 2 seconds (or the step's `timeout_ms`, when that is shorter) for it to go away, logs one WARNING line, writes a `flow.interrupt` journal event, and tries the step again.
A failed action is tried again once, and only after an interception error and a handled interruption, so an action that may already have taken effect is never repeated. An error whose Playwright call log says the action was done (`click action done`), such as a click that went through and then timed out waiting for the next page, is never tried again, even when an earlier attempt in the same log was intercepted.
At most 3 interruptions are handled for one step (one `locator()` call, or one action). If one is still there after that, mendwright raises `AttentionRequired` and tries no heal. That stops an interruption that comes back each time it is closed, for example when its action doesn't close it.

An interruption is never healed by a model. If your script also has a step of its own for it, keep it: `heal.locator("cookies", "#cookie-accept").click()` is skipped when the element isn't there, either because the interruption was handled earlier in the run (no wait) or because it didn't appear this time (after `timeout_ms`). A skipped step logs one INFO line, its proxy has `.present` False, and its actions do nothing. When the banner does come at its old place in the flow, the step clicks it as before.

A skipped step can't tell a banner that didn't appear from one whose selector no longer matches. In the second case the banner stays, and a later action fails because it covers the element. mendwright then logs one WARNING that names the interruptions it looked for (yours, and the `cookies=` preset when it is on), because none of them matched a visible element, and raises the browser's error. The same WARNING comes when the element couldn't be reached for another reason, such as being hidden, while interruptions are registered.

An interruption that hides the element instead of covering it, such as a consent wall that replaces the page until you answer, is found when the step misses, so the step first waits its `timeout_ms`.

### Cookie banners

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

- A popup that needs data before it closes, such as a postcode prompt with no skip. Register its field and then its button with `heal.interrupt()`, which handles them in that order: `heal.interrupt("postcode", "#postcode", action="fill", args=("10115",))`, then `heal.interrupt("postcode_ok", "#postcode-ok")`.
- A popup whose only way out is a button mendwright never clicks, such as Subscribe or OK, or a bare Skip or Dismiss in a popup that doesn't read as a promotion or onboarding. Register the button you want with `heal.interrupt()`.
- A backdrop that is a separate element from the popup's buttons, as some consent managers draw one. Only Escape is tried there, and with `cookies="accept"` not even that. Use `cookies=` or `heal.interrupt()` for those.
- See what a button's own script does. A close or decline button whose click handler loads another page, sends a request or starts a trial is clicked like any other: the `lost` WARNING, when the page changed under the element, is the only sign. Escape, too, reaches every key listener of the page.
- A popup of the page over an iframe that holds the element. mendwright looks in the element's own document, so a popup inside the iframe is handled, and one outside it isn't. Inside an iframe Escape is never pressed, so a popup there that only Escape closes stays.
- A popup that opens while Playwright waits on an action is found only when the action fails, after its timeout.
- A popup that hides the element instead of covering it, such as a wall that replaces the page until you answer. The lookup then misses, and mendwright looks for popups only over an element it found. Register it with `heal.interrupt()`, which is also handled when a lookup misses.
- Actions that don't go through `heal.locator()`, `heal.page` or `heal.driver`. The page or driver you passed to `open()`, and the locators you build from it, get no popup check, and neither do `page.mouse`, `page.keyboard`, `dispatch_event()`, `ElementHandle`s from `query_selector()` or `wait_for_selector()`, `Frame`s from `page.frame()` or `page.frames`, the locators `Locator.all()` returns, page methods that aren't `heal.page` shortcuts (such as `tap()`, `set_checked()` or `drag_and_drop()`), and a shortcut whose selector mendwright can't store. On Selenium, `ActionChains`, and the elements that `execute_script()` or `switch_to.active_element` return, get none. Playwright's overlay handler, which also handles [interruptions](#interruptions) before such actions, isn't used for popups.

On Playwright, with `dismiss=True`, the locators `heal.page` doesn't heal are guarded too: `get_by_role()` and the other `get_by_*` locators, a locator built with options, chained locators, `frame_locator()`, `and_()`, `or_()`, `.first`, `.last`, `.nth()` and `.filter()`.
They come back as `GuardedLocator`s, the real locators with the checks above and the interruption checks before their actions. They aren't healed and nothing about them is saved. They pass `isinstance()` checks, so `expect()` takes them, and `.native` is the real locator. Log lines name their step after the calls that built it, such as `"page.get_by_role('button', name='Continue')"`. With `dismiss=False` they are the real locators, as before.
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

mendwright saves the page where each key's element was found, in the store next to its selectors: `lastPage` (the most recent) and `pages` (the last 5). A page is its path in lower case, without the scheme, host, query or fragment, and without empty parts, so a trailing or doubled slash changes nothing. Parts that look like record ids are replaced by `*`: numbers, uuids, hex of 16 or more characters such as a MongoDB id, and generated ids of 16 or more letters and digits (dashes and underscores allowed) with at least two digits, such as a ULID. So `https://shop.example/Orders/10482/?tab=2` is `/orders/*`, and so is `/orders/507f1f77bcf86cd799439011`. Short parts such as `v2`, `2fa` or `step-3` stay as they are. `about:blank`, `data:` URLs and the site root have no page, and are never compared. A `file://` page is compared by its path, so two local files, such as `v1.html` and `v2.html` of the same page, count as two pages.

When a key's selector misses on a page that isn't one of its saved pages, mendwright declines the heal before any model call, raises `AttentionRequired` and writes a `flow.page_guard` journal event:

```text
'continue' was last found on /signup, and this page is /signup/details. That looks like a change in the flow, not a renamed element, so mendwright did not heal it. If the step moved, update your script. If the URL itself changed in a redesign, pass heal_across_pages=True to mendwright.open().
```

If a redesign only moved the page to a new URL, pass `heal_across_pages=True`. Pass it too when the same steps run under several language paths, such as `/en/checkout` and `/de/checkout`, because those are different pages to the guard. A key that is used on several pages, such as a search box in the header, heals on any of the last 5 pages it was found on. When two processes share a store, as with pytest-xdist, the pages both of them found a key on are kept.

The guard needs a page on both sides, so it doesn't cover a key on its first run, pages loaded with `page.set_content()` (they are `about:blank`), or a step that moved away while the path stays the same. That last case covers apps that route with the URL fragment (`/#/signup`) or the query string (`?page=signup`), and apps that never change the URL. A single-page app that changes the path with the History API is covered. In those cases the model is told it may say that no element fits, and gets the current page and the last page the key was found on when they are known.

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
WARNING mendwright: dismissed a popup in the way of 'email': <div role="dialog" class="nl"> ("Get 10% off your first order. Sign up No thanks") with “No thanks”, after Escape did not close it
WARNING mendwright: left a popup in the way of 'go' alone: <div id="cf" role="alertdialog"> ("Delete this project? Cancel Delete"). It is an alert dialog (role="alertdialog"), which asks for an answer, such as "Delete this project?", and Escape or any of its buttons would give one, so mendwright does not press or click anything on it. The action goes ahead, and fails with the browser's own error if the dialog blocks it. If your script should answer it, register the button with heal.interrupt("<name>", "<selector of the button>").
WARNING mendwright: could not dismiss a popup in the way of 'create': <div id="promo" role="dialog"> ("Join our list. Subscribe Yes, sign me up"). mendwright pressed Escape, which did not close it, and it has no close or decline button that mendwright may click. The action goes ahead, and fails with the browser's own error if the popup blocks it. Register the button that closes it with heal.interrupt("<name>", "<selector of the button>"). If it needs data first, register its field before the button: heal.interrupt("<name>_field", "<selector of the field>", action="fill", args=("<value>",)).
WARNING mendwright: the action on 'email' failed because something covered its element or it could not be reached, and none of the interruptions mendwright looks for (heal.interrupt(): 'cookies') matched a visible element, so it did not try the action again. If the element in the way is one of those registered with heal.interrupt(), its selector no longer matches: update it. Otherwise register the button that closes the element in the way with heal.interrupt().
INFO mendwright: skipped 'cookies': it was already handled as an interruption before 'email', and it is not on the page now.
INFO mendwright: skipped 'promo_close': it is optional (optional=True) and is not on the page.
```

| Event | Fields |
|---|---|
| `flow.interrupt` | `key`, `before` (the step it was handled for, or `<page action>`), `action`, and `selector` or `preset` |
| `flow.dismiss` | `key` (`<popup>`), `before` (the step the popup was in the way of), `popup` (as the WARNING names it), `outcome` (`dismissed`, `failed` when mendwright pressed or clicked something and the popup stayed, or `lost` when the element could not be looked at after a step, or a click raised an error after it may have reached the page), `how` (`escape`, `close` or `decline`, for `dismissed`), `label` (the button's, for `close` and `decline`), `tried` (the steps that did not close it, in order, such as `[{"how": "escape"}, {"how": "close", "label": "Close"}]`: for `dismissed`, those before the one that did) |
| `flow.skip` | `key`, `reason`: `optional`, `interruption handled` or `interruption absent` |
| `flow.page_guard` | `key`, `last_page`, `page` |

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
- `status` is `active`, `quarantined` or `superseded`. Only active candidates are tried.
- Optional per-key fields: `expectedRole`, `expectedText` and `destructive`, `lastPage` and `pages`, the pages where the key's element was found (see [Steps that moved to another page](#steps-that-moved-to-another-page)), `intent` (see [Describing an element with intent=](#describing-an-element-with-intent)), and `fingerprint`, what the element looked like the last time it matched (see [Heals without a model](#heals-without-a-model)).
- A candidate inside an iframe has a `frameXPath` with the frame's selector.
- Up to 5 active candidates per key are kept, most recently successful first.

Writes take a lock file (`overrides.json.lock`), merge with what other processes wrote since the last read, and replace the file atomically.
On a conflict, `quarantined` wins and `destructive: true` wins.
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

The events are `heal.requested`, `heal.proposed`, `heal.rejected` (with a `reason`), `heal.applied`, `heal.canary` and `heal.quarantine`, and for flow changes `flow.interrupt`, `flow.dismiss`, `flow.skip` and `flow.page_guard` (see [Log lines and journal events](#log-lines-and-journal-events)).
`heal.proposed`, `heal.rejected` and `heal.applied` have a `source` of `fingerprint` for a heal from the [fingerprint tier](#heals-without-a-model) (its `provider` is `fingerprint` too) and `intent` for a key found from its [intent=](#describing-an-element-with-intent). `heal.applied` has a `source` and `provider` of `frame`, and a `frame` with the iframe's selector, for a key followed into an [iframe](#iframes). A model heal of a key that had a selector has no `source`.
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

`mendwright` (or `python -m mendwright`) has four commands, plus `--version`.

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

Store (where mendwright.open() saves heals)
  C:\work\shop\.mendwright\overrides.json
  (default, relative to this folder; set MENDWRIGHT_STORE to change it)
  the folder does not exist yet; it is created the first time mendwright.open() records a selector
  C:\work\shop is writable
```

`--json` has a `fingerprint` entry with `enabled` and `source` (`default`, `MENDWRIGHT_FINGERPRINT` or `MENDWRIGHT_DISABLE`).

### mendwright report

Lists each key in a store with its selectors, and the heal counts from the journal.
`--store PATH` picks the file (default `MENDWRIGHT_STORE`, else `./.mendwright/overrides.json`). `--format` is `text`, `md`, `json` or `html`.
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

`--format html` makes one page you can open in a browser or keep as a CI artifact:

```bash
mendwright report --format html --out mendwright-report.html
```

- It has a summary (keys, selectors, quarantined selectors, heals asked for, applied and confirmed, proposals rejected, interruptions, skipped steps and page guard stops), each key with its selectors, the last 25 heals with their old and new selector, state, provider and latency, the last 50 flow events, and the quarantined selectors.
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
| `--include-unconfirmed` | Also write heals that were never confirmed |
| `--apply` | Write the changes |

Which keys it changes:

- The key's most recently successful active selector is a heal, and that heal matched at least once.
- The heal is confirmed: the journal's last `heal.canary` for it is a success. A plain action that worked writes no canary; the pytest plugin writes one when the test that made the heal passes, and so do `expect=` and `confirm(True)`. Rerunning the tests later doesn't confirm an older heal.
- Never a key marked destructive, a heal inside an iframe, or a key whose selectors came only from a seed file.
- Never a key whose old selector is a bare word such as `button`, since other strings in your code (a `role=` value, a dict entry) can be the same word.
- Never a key from `heal.driver`'s `find_element(By.ID, ...)` or `By.NAME`, or from `find_element(By.CSS_SELECTOR, ...)` when the heal has no CSS form, since those calls can't run the healed XPath. The reason says what to change the call to.

Which strings it changes:

- In Python files, only plain string literals whose whole value is the old selector, as the store keeps it (`"xpath=//a"` and `" //a "` both match a stored `//a`). Not an f-string or bytes, not part of an implicit concatenation such as `"a" "b"`, not a comment, not a substring.
- Not the key in `heal.locator(key, selector)` or `heal.interrupt(key, selector)`, and not a `role=`, `text=`, `intent=`, `action=` or `by=` value, even when it equals the selector.
- A CSS selector in your code gets the heal's CSS form when it has one (see [Selectors](#selectors)), also where only CSS runs, such as `By.CSS_SELECTOR` or `querySelector()`. Without a CSS form, a value passed with a Selenium `By` other than `By.XPATH` (in a call, or a `(By.CSS_SELECTOR, "#x")` pair), or to a function with `css` in its name, is listed with the healed selector to put in by hand.
- In other files (`--glob`), quoted strings on one line. Comments are skipped in C-like languages, and template strings with `${` and strings that span lines are left alone. A string right after `By.cssSelector(`, `querySelector(`, `css:` and the like gets the heal's CSS form when it has one, and is otherwise listed, not changed. In languages where only double quotes make a string (Java, Kotlin, C#, Scala, Swift, Go, Rust, JSON), a selector with a double quote in it is escaped, or listed for a hand edit where the escapes aren't known.
- A file in which the same string belongs to more than one key is left alone whole, and listed, because mendwright can't tell which healed selector goes where.

Each rewritten Python file must still parse, have the same structure, and differ only in the planned strings, or it isn't written.
The original quote style and prefix are kept where they can hold the new selector, and a BOM, a coding comment and the line endings are kept.
A file is written through a temporary file next to it and then replaced, and a file that changed since it was read is not overwritten.
`.git`, `.venv`, `venv`, `node_modules`, `.mendwright`, `build`, `dist`, caches and `*.egg-info` folders are skipped, and so are symbolic links, Windows junctions, files over 2 MiB and binary files.

It exits 0 when there is nothing to do, after a dry run and after `--apply`, and 1 when there is no store, a `--path` doesn't exist, or a file couldn't be written.
Once your code holds the healed selector, mendwright tries it first on the next run.

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
| interruptions were handled for '<key>' and '<interruption>' is still on the page | The interruption came back each time it was handled. Check the selector and `action=` you passed to `heal.interrupt()`. |
| does not know what kind of element | Pass `role=` |
| no selector the model proposed passed mendwright's local gate | Read the last reason: `not unique (3)`, `role mismatch`, `not uniquely present on the live page` or `previously quarantined`. Try another model, or fix the selector by hand. |
| the model returned no usable selector | Try a different model, or fix the selector by hand |
| is inside an iframe ... mendwright did not look for it there | The reason says why (a destructive key, several iframes or elements, an iframe inside an iframe, or one nothing tells apart). Open mendwright on the frame (see [Iframes](#iframes)) |
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

### TypeError

| The message says | What to do |
|---|---|
| is an async Playwright object | Use `heal = await mendwright.aio.open(page)` |
| is a sync page or driver, and mendwright.aio.open() takes an async Playwright Page | Use `mendwright.open(page)` |
| points at elements, not at a page | Pass `locator.page`, or a `Frame` from `page.frames` |
| where a selector was expected | Pass a selector string, not a Locator object |
| could not detect a browser driver | Pass a Playwright page or Selenium driver, or `register_driver` your own |
| on Selenium does not support | Selenium ignores only `timeout=`, `force=`, `no_wait_after=` and `delay=`. Remove the other Playwright options. |

### Other problems

- A missing element waits `timeout_ms` (10 seconds by default) before the heal starts. Lower `MENDWRIGHT_TIMEOUT_MS` while you write a script.
- `mendwright doctor` shows which provider and store a run will use.
- A corrupt or unreadable `overrides.json` raises when the store loads. mendwright never treats it as empty and overwrites it. Fix the file or move it aside.
- Healed selectors can contain page text, such as a label. Treat `overrides.json` like any test fixture and review it before you commit it.
- What leaves your machine, and what redaction misses, is in [SECURITY.md](../SECURITY.md).
