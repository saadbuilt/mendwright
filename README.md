<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/saadbuilt/mendwright/main/docs/assets/logo-dark.svg">
  <img src="https://raw.githubusercontent.com/saadbuilt/mendwright/main/docs/assets/logo-light.svg" height="48" alt="mendwright">
</picture>

Self-healing selectors for Playwright and Selenium.

[![PyPI](https://img.shields.io/pypi/v/mendwright)](https://pypi.org/project/mendwright/)
[![Python versions](https://img.shields.io/pypi/pyversions/mendwright?cacheSeconds=3600)](https://pypi.org/project/mendwright/)
[![CI](https://github.com/saadbuilt/mendwright/actions/workflows/ci.yml/badge.svg)](https://github.com/saadbuilt/mendwright/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/saadbuilt/mendwright/blob/main/LICENSE)

</div>

https://github.com/user-attachments/assets/9332fddf-b63f-41d1-b0e5-f03b8f0064e2

<p align="center"><sub>Real runs on a made-up shop, with the waits sped up. The site redeploys and breaks three selectors: mendwright finds two again from memory and one with its 1 GB local model, checks each on the live page and saves them, and the next run takes 0.28 s. Then it rejects a cookie banner, says “No thanks” to a sign-up popup and follows a checkout that asks for payment before shipping. Each of those has a short clip in the <a href="https://github.com/saadbuilt/mendwright/blob/main/docs/guide.md">guide</a>.</sub></p>

mendwright heals many broken selectors without any API key. Each time a selector matches, it saves a fingerprint of the element: its tag, ids, names, label, text and surroundings. When a site update breaks the selector, mendwright looks for that element first, checks the new XPath against the live page, saves it, and retries the step. It asks a model only when the fingerprint match isn't confident. On the next run the saved selector is used and nothing is asked.

For the model it supports Anthropic, OpenAI and compatible APIs, Gemini, Bedrock, LiteLLM, or a local model through Ollama, including mendwright's own small selector model.

## Install

```bash
pip install mendwright
```

## Quickstart

```python
from playwright.sync_api import sync_playwright
import mendwright

with sync_playwright() as p:
    page = p.chromium.launch().new_page()
    page.goto("https://example.com/signup")

    heal = mendwright.open(page)
    heal.locator("email", "#email").fill("ada@example.com")
    heal.locator("accept_terms", "//input[@id='terms']", role="checkbox").check()
    heal.locator("create_account", "#create-account").click()
```

`heal.locator()` takes the selector you'd pass to `page.locator()`, plus a key: a name for the element that its heals are saved under.

`role=` is optional. mendwright needs to know what kind of element it is looking for before it can check a heal, and it learns that the first time the selector matches. Pass `role=` when you add mendwright to a suite whose selectors might already be broken.
`intent=` describes the element in words, such as `intent="the terms checkbox in the signup form"`. The model gets it with the page, and with `role=` and no selector, a model finds the element on first use.

A selector that never matched has no fingerprint, and some changes are beyond one, so to let mendwright ask a model:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export ANTHROPIC_MODEL="..."   # a current model id from Anthropic's model list

# Windows PowerShell:
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:ANTHROPIC_MODEL = "..."
```

There is no default model. `OPENAI_API_KEY` with `OPENAI_MODEL`, `GEMINI_API_KEY` with `GEMINI_MODEL`, and `OLLAMA_MODEL` work the same way, and `MENDWRIGHT_LOCAL_MODEL` selects mendwright's own [local model](#local-model). For Bedrock, LiteLLM or another OpenAI-compatible host, pass `provider=` to `mendwright.open()`.
`mendwright doctor` shows which provider it found. Without one, selectors still resolve and fingerprint heals still happen, and a selector the fingerprint can't place raises an error that lists the variables to set.

A selector that stops matching is waited for 10 seconds (`MENDWRIGHT_TIMEOUT_MS`) before mendwright tries to heal it. Each applied heal prints one line to stderr, and `mendwright report` shows what was saved.

## No changes per step

To heal an existing script without touching its steps, change the line that makes the page or the driver:

<!-- readme-test: wrappers -->
```python
page = mendwright.open(page).page          # Playwright
page.fill("#email", "ada@example.com")
page.check("#terms")

driver = mendwright.open(driver).driver    # Selenium (By from selenium.webdriver.common.by)
driver.find_element(By.ID, "terms").click()
```

Each selector is also the key its heals are saved under. Chained locators, `get_by_*` and the other `By` values go to the real page or driver unhealed, and `.native` is the real object. Interruptions such as a cookie banner (below) are still handled before their actions: on a Playwright `Page` from Playwright 1.42, and before `click()`, `send_keys()`, `clear()` and `submit()` of the elements `heal.driver` returns (not through `.native`). The [guide](https://github.com/saadbuilt/mendwright/blob/main/docs/guide.md#no-changes-per-step) lists what heals.

## Selenium

```python
from selenium import webdriver
import mendwright

driver = webdriver.Chrome()
driver.get("https://example.com/signup")

heal = mendwright.open(driver)
heal.locator("email", "#email").fill("ada@example.com")
heal.locator("accept_terms", "//input[@id='terms']", role="checkbox").check()
driver.quit()
```

Playwright verbs such as `fill()`, `check()` and `select_option()` are mapped to WebElement calls, so step code reads the same on both libraries. Selenium runs XPath and CSS, so Playwright selectors such as `text=Save` raise `InvalidSelector` at once.

## Async Playwright

```python
import asyncio
from playwright.async_api import async_playwright
import mendwright.aio

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto("https://example.com/signup")

        heal = await mendwright.aio.open(page)
        await heal.locator("accept_terms", "//input[@id='terms']", role="checkbox").check()

asyncio.run(main())
```

`heal.locator()` returns at once. The element is resolved, and healed if needed, when you await an action on it. The model call runs in a worker thread and doesn't block the event loop.

## pytest

```python
def test_signup(page, heal):
    page.goto("https://example.com/signup")
    heal.locator("email", "#email").fill("ada@example.com")
    heal.locator("accept_terms", "//input[@id='terms']", role="checkbox").check()
```

The `heal` fixture comes from mendwright's pytest plugin, which pytest loads on its own once mendwright is installed. It wraps the `page` fixture from pytest-playwright (`pip install pytest-playwright`, then `python -m playwright install chromium`). For Selenium, define a `driver` fixture that yields your WebDriver and set `mendwright_page_fixture = driver` in your pytest config.

When a test passes, its heals are marked confirmed. When it fails, they stay saved (and are still used next run) but unconfirmed, and the failure report lists them so you can check whether a heal picked the wrong element.

To make CI show selector drift instead of quietly healing it, run `pytest --mendwright-strict`. A test that needed a heal then fails with the old and the new selector, and the heal is still saved.

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/saadbuilt/mendwright/main/docs/assets/how-it-works-dark.svg">
  <img src="https://raw.githubusercontent.com/saadbuilt/mendwright/main/docs/assets/how-it-works-light.svg" width="800" alt="How a heal works: a selector misses; a key marked destructive, an unknown role or a page the key was never found on stops with AttentionRequired before anything is tried; otherwise the page is captured, and an XPath comes from the saved fingerprint when one element matches it with confidence, else from the model within the hourly limit; local checks run; a failed fingerprint pick goes to the model, a failed model pick gets the reason and one more try, and a second failure raises AttentionRequired with nothing saved; a pass is saved and the step retried; if the action then fails the healed selector is quarantined.">
</picture>

A step is refused before anything is tried when its key is marked destructive, when mendwright doesn't know what kind of element it is (see `role=` above), or when the page isn't one the key was found on. mendwright doesn't guess which controls are destructive. Mark a pay, submit or delete step with `heal.locator(..., destructive=True)`, or pass its key in `mendwright.open(destructive_keys=...)`.
Next, mendwright scores the page's elements against the key's fingerprint. It takes the best one, with no model call, only when it shares an id, test id, name, link, label or text with the recorded element and no other element comes close.
Otherwise it sends a cleaned, redacted copy of the page to the model (at most 60,000 characters of HTML and 60,000 of element inventory per request; on a longer page, the parts that look unrelated to the step are left out first), within an hourly limit of 3 heal attempts per key and 60 in all. A rejected attempt counts, and an attempt makes up to 2 model calls. A gateway meters on its side instead.
Either way, the XPath is used only if:

- it isn't an index-only path such as `/html[1]/body[1]/div[3]/input[1]`
- the store doesn't hold it as quarantined or superseded for this key, and it doesn't reach an element the key has quarantined
- it matches exactly one element in the captured HTML
- that element has the step's role (checkbox, button, link, and so on)
- it matches exactly one element on the live page, and that element is visible (hidden checkboxes are allowed) and still has the role

If a fingerprint pick fails, the model is asked. If the model's first proposal fails, it gets the reason and one more try. Heals are saved in `.mendwright/overrides.json`, and each request, proposal, rejection and outcome goes to `journal.jsonl` next to it.
On the next run the selector in your code is tried first and the saved heal right after it, in the same pass. That resolves without a wait or a model call. If the action on a healed element fails in the browser, the heal is quarantined and not used again.

## Local model

mendwright has its own small model, so a heal can run without the page leaving your machine.
`mendwright-selector` is Qwen2.5-1.5B-Instruct fine-tuned for one question: given the broken step and a numbered list of the elements of its role on the current page, which one did the step mean, if any.
It answers with a number, or says that no element fits and why. mendwright then builds the XPath for that element itself and runs it through the same checks as any heal, so the model never writes a selector.

With [Ollama](https://ollama.com/download) installed, one command downloads the model from the GitHub release, checks its SHA-256 and registers it, and one variable selects it:

```bash
mendwright model install
export MENDWRIGHT_LOCAL_MODEL=mendwright-selector
```

To build it yourself instead, [training/README.md](https://github.com/saadbuilt/mendwright/blob/main/training/README.md) has the data generator and the recipe (about 6 hours of training on a laptop GPU with 6 GB).

The model file is about 1 GB. It answers in about a quarter of a second on a laptop GPU, or 1 to 2 seconds on the CPU alone.
On 3,000 held-out synthetic redesigns it picked the right element for 97.1% of the broken selectors and a wrong one for 0.14%, and said no element fits for 97.1% of the steps where none did.
A pick it is less than 80% sure of is declined rather than used (`MENDWRIGHT_LOCAL_MIN_CONFIDENCE` changes that).
On the [47-case benchmark](https://github.com/saadbuilt/mendwright/blob/main/docs/benchmark.md), behind the fingerprint tier, it healed 30 of 35 broken selectors correctly in headless Chromium (31 on the stand-in page) and none onto the wrong element, and declined 11 of the 12 cases where the right answer is no heal ([results](https://github.com/saadbuilt/mendwright/blob/main/benchmarks/RESULTS.md)). Its last training round was shaped by its failures on those same cases, so expect it to do a little worse on your pages.

## What it can and can't fix

mendwright repairs a selector on the page your script is on. It handles layout changes within that page:

- ids and classes renamed, or replaced by build-generated ones
- labels reworded or translated (with a model)
- elements wrapped in a new container or component
- elements moved to another part of the page, or form fields put in a different order
- lookalike elements where the surrounding text decides which one is meant

It also handles some changes in your script's flow:

- A step that can show up at any point, such as a cookie banner, a newsletter modal or an "are you still there?" dialog. Register it once with `heal.interrupt()`. When it is in the way of a step (the step's element is missing, or the page shows the interruption), mendwright handles it first (a click, unless you register another action, or a function of yours with `run=` for one that needs a form, such as a sign-in after the session expired) and goes on with the step. If your script also has its own step for it, that step is skipped once the interruption was handled, so a cookie banner that now appears before signup instead of after it needs no other change.
  This also covers actions that don't go through `heal.locator()`. On Playwright 1.42 and later mendwright registers the interruption with `page.add_locator_handler`, so Playwright hands over to it before a `get_by_*` or chained locator acts too, and while an action waits (not before `page.mouse`, `page.keyboard` or an action with `force=True`). On Selenium the elements `heal.driver` finds check for it before `click()`, `send_keys()`, `clear()` and `submit()` (not `ActionChains`, and only inside the frame after your own `switch_to.frame()`).
- The consent banners of common consent managers, with `cookies="reject"` or `cookies="accept"`. This is off by default.
- Popups you never registered, such as a newsletter modal, a survey or a chat invite, with `dismiss=True`. When one covers the element of an action, mendwright presses Escape (only while the keyboard focus is on the page or in the popup, so a field you filled keeps its text), then clicks the popup's close button (Close, or an icon named Close, never a bare × that in a cart removes an item), then a button such as No thanks or Not now, and looks again after each. It never clicks a button that says Subscribe, Accept, Yes or Continue (unless it says Continue without, as in Continue without an account), a link that goes somewhere, or a button that submits a form, it never types into the popup, and it leaves a dialog that asks a question alone, such as an alert dialog or one with a Cancel button. A popup it can't dismiss is named in a WARNING and the action fails with its own error. This is off by default. The [guide](https://github.com/saadbuilt/mendwright/blob/main/docs/guide.md#popups-mendwright-wasnt-told-about) lists what counts as a popup and as each button.
- A step that may not be there, with `heal.locator(..., optional=True)`.
- Steps the site shows in another order, puts on one page, or leaves out, such as a checkout that now asks for the card before the address. Register each step with `heal.step(name, when=...)`, where `when` is something only that step shows, such as its heading (it is never healed, so pick one that stays), and `heal.run_steps()` runs whichever step the site shows, each once, until the one marked `last=True`. A step the site left out is then named in a WARNING. A step that is on the page again after it ran, such as payment after a declined card, or a page where no step shows, stops the flow with an error instead of a guess. The [guide](https://github.com/saadbuilt/mendwright/blob/main/docs/guide.md#steps-in-any-order) has an example.

<!-- readme-test: flow -->
```python
heal = mendwright.open(page, cookies="reject")      # close common consent banners
heal.interrupt("newsletter", "#newsletter-close")   # your own modal, wherever it opens

heal.locator("email", "#email").fill("ada@example.com")
heal.locator("create_account", "#create-account").click()
```

Outside `heal.run_steps()`, a step that moved to another page and steps in a new order need a change to your script. With `heal.run_steps()`, steps in a new order or on one page don't, and nor does a step left out when the final step has `last=True`. A new required step, an action that moved from one step to another, and a step whose landmark changed (landmarks are never healed) still do.
mendwright remembers the last few pages each key was found on and doesn't heal a key on another page (inside `heal.run_steps()`, the page of the step it was found during counts as its own), so you get an error that says the flow changed, instead of a heal onto a similar element such as a Help button where a Continue button used to be. If a redesign only changed the URL, or the same steps run under language paths such as `/en/` and `/de/`, pass `heal_across_pages=True` to `mendwright.open()`.
The model is also told it may answer that no element on the page fits, but a step that moved within the same address is still the model's call, so mark irreversible steps with `destructive=True`. The [guide](https://github.com/saadbuilt/mendwright/blob/main/docs/guide.md#flow-changes) has the details.

Anchor selectors on ids, names, labels or text rather than position. A positional selector such as `(//form//input)[1]` that still matches after two fields swap now points at the wrong field, and since nothing broke, mendwright doesn't step in.

## Errors

When mendwright can't or won't heal a step, it raises `mendwright.AttentionRequired` with the reason and a next step. This is the error from `heal.locator("pay", "#pay", destructive=True)` when `#pay` stopped matching:

<!-- readme-test: attention-pay -->
```text
mendwright.exceptions.AttentionRequired: No matching element for: pay (tried 1 candidate(s)).
  -> 'pay' is marked as a destructive control, and mendwright never auto-heals those, because a validated but wrong selector here could act on a live account. Fix the selector in your code or seed file by hand.
```

`mendwright doctor` checks the install, the provider and the store path. `mendwright report` lists each key with its selectors and recent heals; `--format md` prints a Markdown table you can append to `$GITHUB_STEP_SUMMARY`, and `--format html --out report.html` writes one self-contained page.
`mendwright fix` puts confirmed heals back into your code: it prints a diff of the string literals it would change, and `--apply` writes them.

## Compatibility

| Area | Supported |
|---|---|
| Browser libraries | Playwright sync and async (Chromium, Firefox, WebKit), patchright, camoufox, Selenium (Chrome, Firefox), or your own driver through `register_driver` |
| Selectors in your code | XPath, CSS, and Playwright selectors such as `role=button[name='Save']` (Playwright only) |
| Selectors mendwright saves | XPath |
| Model providers | Anthropic, OpenAI, Azure OpenAI, OpenAI-compatible hosts (Groq, OpenRouter, vLLM, LM Studio), Ollama, Gemini, Bedrock, LiteLLM, your own gateway, or mendwright's own selector model in Ollama |
| Python | 3.10 and newer, with lxml as the only dependency |

The real-browser suite (`python -m pytest -m browser`) passed locally on Windows 11 with Python 3.12 on Playwright Chromium, Firefox and WebKit, patchright, camoufox, and Selenium with Chrome and Firefox. The async tests passed on all three Playwright browsers. CI runs the offline suite on Linux (Python 3.10 to 3.14), Windows and macOS, and the browser suite on Linux.

## Comparison

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/saadbuilt/mendwright/main/docs/assets/feature-matrix-dark.svg">
  <img src="https://raw.githubusercontent.com/saadbuilt/mendwright/main/docs/assets/feature-matrix-light.svg" width="800" alt="Feature comparison of mendwright, Healenium, robotframework-heal, Skyvern, Stagehand and auto-playwright across thirteen features, including two where mendwright does not lead. The same data is in docs/comparison.md.">
</picture>

Where another tool fits better: Healenium covers Java, and any Selenium client through its proxy, and its open source edition never calls a model in any setup. Robot Framework suites have robotframework-heal. Agent tools such as Stagehand and Skyvern handle steps written in plain English. [docs/comparison.md](https://github.com/saadbuilt/mendwright/blob/main/docs/comparison.md) has the full comparison, with a source for every cell and the footprint charts.

## Limitations

- Heals run as XPath. When your code's selector is CSS, the heal also gets a CSS form that `mendwright fix` writes back, but only if CSS can tell the element apart: one that only its text identifies stays XPath, and so does a heal for a Playwright `text=` or `role=` selector.
- Iframes work one level down: a selector that matches only inside an iframe is looked up there, and a key found there heals there. That works from the page, and on Playwright also from a `Frame` mendwright was opened on, but not from a Selenium driver after `switch_to.frame()`. An iframe inside another iframe, a key marked destructive, a match in several iframes, or an iframe with no id, name, title or `src` still needs mendwright opened on the frame (a `Frame` from `page.frames`, or the driver after `switch_to.frame()`).
- With a hosted provider, page HTML is sent to it whenever a fingerprint match isn't confident. Redaction removes quoted `value` attributes, absolute http(s) URLs and runs of 12 to 19 digits, but visible text and other attributes stay. See [SECURITY.md](https://github.com/saadbuilt/mendwright/blob/main/SECURITY.md). A local model through Ollama keeps the page on your machine.
- If a page's cleaned HTML is longer than 60,000 characters, a hosted model gets a shortened copy. Parts that share no word with the step and aren't of its role are left out first, so the element can still be left out on a page with more related elements than fit. While parts are left out, a healed XPath can't use a position such as `[2]`.
- Accuracy has been measured without a hosted model only. On the [47-case benchmark](https://github.com/saadbuilt/mendwright/blob/main/docs/benchmark.md), the fingerprint tier alone healed 20 of 35 broken selectors and none onto a wrong element, but its thresholds were tuned on those same cases, so expect it to be wrong now and then on yours. With a model-free similarity baseline behind it, 28 of 35 healed, and 3 of the 47 cases went to the wrong element. With mendwright's own [local model](#local-model) behind it, 30 of 35 healed in headless Chromium (31 on the stand-in page) and 1 of the 47 went to the wrong element ([results](https://github.com/saadbuilt/mendwright/blob/main/benchmarks/RESULTS.md)). No hosted model numbers are published yet.
- `--mendwright-strict` only fails the run that made the heal. Later runs pick up the saved heal and pass, which is why strict CI runs need a fresh store.

## Links

- [Guide](https://github.com/saadbuilt/mendwright/blob/main/docs/guide.md): configuration, the pytest plugin, CI recipes, the CLI and troubleshooting
- [Comparison](https://github.com/saadbuilt/mendwright/blob/main/docs/comparison.md)
- [Contributing](https://github.com/saadbuilt/mendwright/blob/main/CONTRIBUTING.md)
- [Security](https://github.com/saadbuilt/mendwright/blob/main/SECURITY.md)
- [Changelog](https://github.com/saadbuilt/mendwright/blob/main/CHANGELOG.md)

[MIT license](https://github.com/saadbuilt/mendwright/blob/main/LICENSE). If mendwright fixed a broken run for you, a star helps other people find it.
