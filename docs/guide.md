# mendwright guide

The [README](../README.md) gets you to a first heal.
This page covers the rest: settings, providers, the pytest plugin, async and Selenium details, the files mendwright writes, CI, the command line and troubleshooting.

Contents:

- [Configuration](#configuration)
- [Providers](#providers)
- [pytest plugin](#pytest-plugin)
- [Async Playwright](#async-playwright)
- [Selenium](#selenium)
- [Iframes](#iframes)
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
| `provider` | `auto_provider()` | The model provider. If none can be built, selectors still resolve, heals are declined with the reason, and one WARNING is logged. |
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

The two rates are shared by every Healer in one process that uses the same store file and the same two rate values.
`GatewayProvider` gets no client-side limit, because the gateway meters on its side.

### Healer.locator()

```python
heal.locator(key, selector=None, *, role=None, text=None, destructive=None,
             timeout_ms=None, poll_ms=200)
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

It returns an `ActionProxy` once the element is found, healed or not.
It raises `AttentionRequired` when the element can't be found or healed, `InvalidSelector` or `TypeError` for a selector that can never work, and `ValueError` for an unknown role.

Other `Healer` members:

- `heal.present(key)`: True if a stored selector matches one element at the moment of the call. It doesn't wait or heal.
- `heal.heals`: the heals this Healer applied, as `HealEvent(key, old, new, confirmed)`.
- `heal.pending()`: `{key: xpath}` for heals that aren't confirmed yet.
- `heal.confirm_all(True)` or `heal.confirm_all(False)`: confirm or quarantine every pending heal.

### Actions and confirmation

The proxy runs these actions through the driver: `click`, `dblclick`, `fill`, `type`, `press`, `press_sequentially`, `check`, `uncheck`, `set_checked`, `select_option`, `hover`, `tap`, `clear`, `focus`, `set_input_files`, `drag_to`, `send_keys` and `submit`.
Keyword arguments pass through, and the return value is what the native call returned.
Anything else, such as `inner_text()`, `get_attribute()` or `is_checked()`, goes to the native object unchanged.
`proxy.native` is the Playwright `Locator` or Selenium `WebElement` itself, for APIs that check the type:

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
A quarantined selector stays in the file with status `quarantined`. It isn't tried again, and a later proposal for that key is rejected if it reaches the same element, even through a different XPath.
`AttributeError`, `TypeError` and `ValueError` from a wrong verb or argument quarantine nothing.

Confirming writes a `heal.canary` line to the journal and calls the provider's `report_outcome()`. The gateway uses that to record outcomes.
An unconfirmed heal stays active and is used on later runs.

### Environment variables

| Variable | Meaning |
|---|---|
| `MENDWRIGHT_STORE` | Store path, the default for `store_path` |
| `MENDWRIGHT_TIMEOUT_MS` | Default `timeout_ms`, in whole milliseconds |
| `MENDWRIGHT_DRY_RUN` | `1` turns dry run on |
| `MENDWRIGHT_DISABLE` | `1` resolves selectors but never heals or calls a provider, even one passed as `provider=` |
| `MENDWRIGHT_PROVIDER` | `anthropic`, `openai`, `gemini`, `gateway` or `ollama`: use that provider instead of detecting one |
| `MENDWRIGHT_GATEWAY` | Gateway URL, which selects `GatewayProvider` |
| `MENDWRIGHT_KEY` | The client key the gateway checks |
| `MENDWRIGHT_AUTH_HEADER` | The header that carries it, `X-Heal-Key` by default |
| `MENDWRIGHT_REQUIRE_ENGINES` | Only for this repo's browser tests: comma-separated engine ids that must start, so CI fails instead of skipping |

Flags take `1`, `true`, `yes` or `on`, and `0`, `false`, `no`, `off` or empty.
Any other value makes `open()` raise `ValueError` (for `MENDWRIGHT_DRY_RUN`, only when `dry_run=` isn't passed).

## Providers

### How one is picked

With no `provider=`, `auto_provider()` reads the environment and takes the first match:

| Order | Set this | Provider | Model from |
|---|---|---|---|
| 1 | `ANTHROPIC_API_KEY` | `AnthropicProvider` | `ANTHROPIC_MODEL` |
| 2 | `OPENAI_API_KEY` | `OpenAICompatibleProvider` (api.openai.com) | `OPENAI_MODEL` |
| 3 | `GEMINI_API_KEY` | `GeminiProvider` | `GEMINI_MODEL` |
| 4 | `MENDWRIGHT_GATEWAY` | `GatewayProvider` | the gateway |
| 5 | `OLLAMA_MODEL` or `OLLAMA_HOST` | Ollama, keyless | `OLLAMA_MODEL` |

`OLLAMA_HOST` defaults to `http://localhost:11434`.
`MENDWRIGHT_PROVIDER` skips the order and forces one of the five.
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
                        LiteLLMProvider, OpenAICompatibleProvider)

provider = AnthropicProvider(model="...")             # key from ANTHROPIC_API_KEY
provider = OpenAICompatibleProvider(model="...")      # OpenAI, key from OPENAI_API_KEY
provider = OpenAICompatibleProvider(model="...", base_url="https://api.groq.com/openai/v1", api_key="...")
provider = OpenAICompatibleProvider.for_ollama("...")  # http://localhost:11434/v1, no key
provider = OpenAICompatibleProvider.for_azure(endpoint, deployment, api_version, key)
provider = GeminiProvider(model="...")                # key from GEMINI_API_KEY
provider = BedrockProvider(model="...", region="us-east-1")  # AWS credentials from boto3
provider = LiteLLMProvider(model="...")               # any model string LiteLLM routes
provider = GatewayProvider("https://heal.example.workers.dev", "client-key")

heal = mendwright.open(page, provider=provider)
```

`BedrockProvider` and `LiteLLMProvider` also read `BEDROCK_MODEL` and `AWS_REGION`, or `LITELLM_MODEL`, `LITELLM_API_BASE` and `LITELLM_API_KEY`.

Vendor SDKs are optional.
Anthropic, OpenAI-compatible hosts and Gemini fall back to a plain HTTPS call through the standard library when their SDK is missing.
Bedrock needs boto3: `pip install "mendwright[bedrock]"`.
LiteLLM needs the litellm package (`pip install "mendwright[litellm]"`), or an `api_base` that points at an OpenAI-compatible LiteLLM proxy.
The other extras are `[anthropic]`, `[openai]`, `[gemini]` and `[all]`. `[all]` installs every provider SDK and no browser library.

Any object with `propose(bundle)` and `report_outcome(key, xpath, success)` works as a provider. [CONTRIBUTING.md](../CONTRIBUTING.md) describes the contract.

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

Selectors in frames resolve when a stored candidate carries a `frameXPath` (see [Store and journal](#store-and-journal)).
Healing is different: the capture and the checks run on one document, so a heal inside an iframe needs mendwright opened on the frame itself.

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

If a selector matches nothing on the page but would match inside a child iframe, Playwright raises `AttentionRequired` and says so. It makes no model call, because a heal would be checked against the wrong document. Open mendwright on the frame as above.

Both cases are covered by the real-browser test `test_the_iframe_workaround`, which also checks that the captured HTML is the frame's.

## Layout changes and flow changes

A heal happens when a stored selector stops matching on the page mendwright was opened on. That makes it a fit for layout changes inside one page: renamed or generated ids and classes, reworded or translated labels, new wrapper elements, elements moved to another region, and form fields in a new order. The benchmark has cases for each of these ([docs/benchmark.md](benchmark.md)).

Flow changes are different. When a step moves to the next page, a confirmation modal is added, or two steps swap order, the element your step wants is not on the current page. mendwright does not navigate or reorder your script. It asks the model for a replacement on the page it has, and a model may propose a similar element that is unique, visible and of the right role, for example a Help button in place of a Continue button that moved away. That passes the checks. Two things limit the damage:

- Mark steps that submit, pay or delete with `destructive=True`, so they are never healed (see [Destructive controls](#destructive-controls)).
- Run CI with `--mendwright-strict`, so a heal fails the test and someone looks at it before it is trusted (see [Strict mode](#strict-mode)).

Positional selectors are a blind spot. `(//form//input)[1]` keeps matching after the email and password fields swap places, so it now points at the password field. No selector broke, so mendwright never runs. Anchor selectors on something that travels with the element: an id, a name, a label or its text.

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

Heals are always XPath.

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
- Optional per-key fields: `expectedRole`, `expectedText` and `destructive`.
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

The events are `heal.requested`, `heal.proposed`, `heal.rejected` (with a `reason`), `heal.applied`, `heal.canary` and `heal.quarantine`.
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
A declined heal logs one WARNING too, or an INFO line in dry run. With a format that shows the level, they look like this:

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

`mendwright` (or `python -m mendwright`) has three commands, plus `--version`.

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
  none configured, so a broken selector is reported but not repaired.
  Why: mendwright.auto_provider: no provider configured. Set one of these, checked in
  this order: ANTHROPIC_API_KEY and ANTHROPIC_MODEL (Anthropic); OPENAI_API_KEY and
  OPENAI_MODEL (OpenAI-compatible); GEMINI_API_KEY and GEMINI_MODEL (Gemini);
  MENDWRIGHT_GATEWAY and MENDWRIGHT_KEY, plus MENDWRIGHT_AUTH_HEADER if your gateway
  uses another header (mendwright gateway); OLLAMA_MODEL, plus OLLAMA_HOST if Ollama is
  not on http://localhost:11434 (local Ollama). Or set MENDWRIGHT_PROVIDER to one of:
  anthropic, openai, gemini, gateway, ollama, or pass provider=... to mendwright.open().
  `mendwright demo` works without one (it uses a scripted reply).

Store (where mendwright.open() saves heals)
  C:\work\shop\.mendwright\overrides.json
  (default, relative to this folder; set MENDWRIGHT_STORE to change it)
  the folder does not exist yet; it is created on the first heal
  C:\work\shop is writable
```

### mendwright report

Lists each key in a store with its selectors, and the heal counts from the journal.
`--store PATH` picks the file (default `MENDWRIGHT_STORE`, else `./.mendwright/overrides.json`). `--format` is `text`, `md` or `json`.

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
- `HealCoordinator` writes no journal and has no rate limit unless you pass `journal=` and `guardrails=`.

## Troubleshooting

### AttentionRequired

The first line names the key. The line after `->` says why there was no heal:

| The message says | What to do |
|---|---|
| is marked as a destructive control | Fix the selector by hand. If you marked it with `destructive=True` and the control is safe to heal, pass `destructive=False` |
| no provider configured | Set a key and its model variable, or pass `provider=` |
| `<Provider> returned no selector for '<key>': <reason>` | The reason is the provider's own error, such as an HTTP 401 for a bad key, an unknown model or no network. Fix what it names, or try another model. |
| does not know what kind of element | Pass `role=` |
| no selector the model proposed passed mendwright's local gate | Read the last reason: `not unique (3)`, `role mismatch`, `not uniquely present on the live page` or `previously quarantined`. Try another model, or fix the selector by hand. |
| the model returned no usable selector | Try a different model, or fix the selector by hand |
| the selector matches only inside a child iframe (Playwright) | Open mendwright on the frame (see [Iframes](#iframes)) |
| rate/spend cap was reached | Wait for the hourly window, or raise `rate_per_key_per_hour` and `rate_global_per_hour` |
| dry-run mode is on | Unset `MENDWRIGHT_DRY_RUN`, or drop `dry_run=True` or `--mendwright-dry-run` |
| turned off by MENDWRIGHT_DISABLE=1 | Unset it, or drop `--mendwright-disable` |
| a transaction guard is currently blocking heals | Your `guard=` returned False. Run the step again once it allows heals. |
| there is no stored selector | Pass a selector on the first call for that key |

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
