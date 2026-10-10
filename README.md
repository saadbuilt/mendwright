<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/saadbuilt/mendwright/main/docs/assets/logo-dark.svg">
  <img src="https://raw.githubusercontent.com/saadbuilt/mendwright/main/docs/assets/logo-light.svg" height="48" alt="mendwright">
</picture>

Self-healing selectors for Playwright and Selenium.

[![PyPI](https://img.shields.io/pypi/v/mendwright)](https://pypi.org/project/mendwright/)
[![Python versions](https://img.shields.io/pypi/pyversions/mendwright)](https://pypi.org/project/mendwright/)
[![CI](https://github.com/saadbuilt/mendwright/actions/workflows/ci.yml/badge.svg)](https://github.com/saadbuilt/mendwright/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/saadbuilt/mendwright/blob/main/LICENSE)

</div>

When a site update breaks a selector, mendwright asks a model for a new XPath, checks it against the live page, saves it, and retries the step. On the next run the saved selector is used and the model isn't called at all.

Use the model you already have: Anthropic, OpenAI and compatible APIs, Gemini, Bedrock, LiteLLM, or a local model through Ollama.

## Install

```bash
pip install mendwright
mendwright demo
```

If the `mendwright` command isn't on your PATH (common on Windows), run `python -m mendwright demo`.

The demo runs offline, with no browser and no API key. A fake page built on lxml stands in for the browser and a canned reply stands in for the model. A redesign breaks two selectors. mendwright heals the checkbox, and refuses to heal the pay button because the demo marks it `destructive=True`.

<img src="https://raw.githubusercontent.com/saadbuilt/mendwright/main/docs/assets/demo.svg" width="100%" alt="Terminal recording of mendwright demo. On v2 of a checkout page the accept_terms selector misses, the model proposes an XPath anchored on the label text, it passes the checks and the box is checked. The pay selector, marked destructive=True, also misses and is refused without a model call.">

<details>
<summary>The same output as text</summary>

```text
mendwright demo: a redesign breaks two selectors on a checkout page.
Offline: a stand-in page (lxml, no browser) and a scripted model reply (no API call).

  v1  accept_terms  #accept-terms  ok     matched, box checked
  v1  pay           #pay           ok     matched (not clicked)
      -- v2: a redesign ships and both ids are gone --
  v2  accept_terms  #accept-terms  MISS   no element matches
      model    proposed //label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox'] (scripted reply, no API call)
      checks   pass: 1 checkbox in the captured HTML, 1 match on the live page
      retry    ok: box checked with the healed selector, heal confirmed
  v2  pay           #pay           MISS   no element matches
      refused  'pay' is marked destructive=True. Not healed, no model call.

Done: 1 heal applied and confirmed, 1 refused, 1 model call in total.
```

</details>

`mendwright demo --browser chromium` runs the same demo in a real headless browser. It needs `pip install "mendwright[playwright]"` and `python -m playwright install chromium`. `mendwright demo --live` asks the model from your environment (set it up as shown below) instead of the canned reply. Only the demo page is sent.

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

Then tell mendwright which model to ask:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export ANTHROPIC_MODEL="..."   # a current model id from Anthropic's model list

# Windows PowerShell:
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:ANTHROPIC_MODEL = "..."
```

There is no default model. `OPENAI_API_KEY` with `OPENAI_MODEL`, `GEMINI_API_KEY` with `GEMINI_MODEL`, and `OLLAMA_MODEL` work the same way.
For Bedrock, LiteLLM or another OpenAI-compatible host, pass `provider=` to `mendwright.open()`.
`mendwright doctor` shows which provider it found. Without one, selectors still resolve, and a broken one raises an error that lists the variables to set.

A selector that stops matching is waited for 10 seconds (`MENDWRIGHT_TIMEOUT_MS`) before mendwright asks the model. Each applied heal prints one line to stderr, and `mendwright report` shows what was saved.

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

Playwright verbs such as `fill()`, `check()` and `select_option()` are mapped to WebElement calls, so step code reads the same on both libraries.
Selenium runs XPath and CSS, so Playwright selectors such as `text=Save` raise `InvalidSelector` at once.

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
  <img src="https://raw.githubusercontent.com/saadbuilt/mendwright/main/docs/assets/how-it-works-light.svg" width="800" alt="How a heal works: a selector misses; a key marked destructive, an unknown role or a used-up hourly limit stops with AttentionRequired before any model call; otherwise the page is captured and redacted, a model proposes an XPath and local checks run; a failure gives the model the reason and one more try, and a second failure raises AttentionRequired with nothing saved; a pass is saved and the step retried; if the action then fails the healed selector is quarantined.">
</picture>

A step is refused before any model call when its key is marked destructive, when mendwright doesn't know what kind of element it is (see `role=` above), or when the hourly limit is used up: 3 heal attempts per key and 60 in all by default.
mendwright doesn't guess which controls are destructive. Mark a pay, submit or delete step with `heal.locator(..., destructive=True)`, or pass its key in `mendwright.open(destructive_keys=...)`.
A rejected attempt counts, and an attempt makes up to 2 model calls. Going through a gateway skips this limit, because the gateway meters on its side.
Otherwise mendwright sends a cleaned, redacted copy of the page to the model (at most 60,000 characters of HTML and 60,000 of element inventory per request), and uses the XPath it proposes only if:

- it isn't an index-only path such as `/html[1]/body[1]/div[3]/input[1]`
- the store doesn't hold it as quarantined or superseded for this key, and it doesn't reach an element the key has quarantined
- it matches exactly one element in the captured HTML
- that element has the step's role (checkbox, button, link, and so on)
- it matches exactly one element on the live page, and that element is visible (hidden checkboxes are allowed) and still has the role

If the first proposal fails, the model gets the reason and one more try. Heals are saved in `.mendwright/overrides.json`, and each request, proposal, rejection and outcome goes to `journal.jsonl` next to it.
On the next run the selector in your code is tried first and the saved heal right after it, in the same pass. That resolves without a wait or a model call. If the action on a healed element fails in the browser, the heal is quarantined and not used again.

## Errors

When mendwright can't or won't heal a step, it raises `mendwright.AttentionRequired` with the reason and a next step.
This is the error from `heal.locator("pay", "#pay", destructive=True)` when `#pay` stopped matching:

<!-- readme-test: attention-pay -->
```text
mendwright.exceptions.AttentionRequired: No matching element for: pay (tried 1 candidate(s)).
  -> 'pay' is marked as a destructive control, and mendwright never auto-heals those, because a validated but wrong selector here could act on a live account. Fix the selector in your code or seed file by hand.
```

`mendwright doctor` checks the install, the provider and the store path. `mendwright report` lists each key with its selectors and recent heals; `--format md` prints a Markdown table you can append to `$GITHUB_STEP_SUMMARY`.

## Compatibility

| Area | Supported |
|---|---|
| Browser libraries | Playwright sync and async (Chromium, Firefox, WebKit), patchright, camoufox, Selenium (Chrome, Firefox), or your own driver through `register_driver` |
| Selectors in your code | XPath, CSS, and Playwright selectors such as `role=button[name='Save']` (Playwright only) |
| Selectors mendwright saves | XPath |
| Model providers | Anthropic, OpenAI, Azure OpenAI, OpenAI-compatible hosts (Groq, OpenRouter, vLLM, LM Studio), Ollama, Gemini, Bedrock, LiteLLM, or your own gateway |
| Python | 3.10 and newer, with lxml as the only dependency |

The real-browser suite (`python -m pytest -m browser`) passed locally on Windows 11 with Python 3.12 on Playwright Chromium, Firefox and WebKit, patchright, camoufox, and Selenium with Chrome and Firefox. The async tests passed on all three Playwright browsers.
CI runs the offline suite on Linux (Python 3.10 to 3.14), Windows and macOS, and the browser suite on Linux.

## Comparison

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/saadbuilt/mendwright/main/docs/assets/feature-matrix-dark.svg">
  <img src="https://raw.githubusercontent.com/saadbuilt/mendwright/main/docs/assets/feature-matrix-light.svg" width="800" alt="Feature comparison of mendwright, Healenium, robotframework-heal, Skyvern, Stagehand and auto-playwright across thirteen features, including four where mendwright does not lead. The same data is in docs/comparison.md.">
</picture>

Where another tool fits better: Healenium needs no change per step (you wrap the Java driver once, or point a Python, JS or C# Selenium client at its proxy), and its open source edition calls no model, which keeps page content on your machine. Robot Framework suites have robotframework-heal. Agent tools such as Stagehand and Skyvern handle steps that have no selector at all.
[docs/comparison.md](https://github.com/saadbuilt/mendwright/blob/main/docs/comparison.md) has the full comparison, with a source for every cell and the footprint charts.

## Limitations

- Heals are always XPath, even when your code used CSS or a Playwright selector.
- An element inside an iframe heals only when you open mendwright on the frame: a `Frame` from `page.frames` on Playwright, or the driver after `switch_to.frame()` on Selenium. On Playwright, a selector that matches only inside a child iframe raises `AttentionRequired` and says so, without a model call.
- With a hosted provider, page HTML is sent to it. Redaction removes quoted `value` attributes, absolute http(s) URLs and runs of 12 to 19 digits, but visible text and other attributes stay. See [SECURITY.md](https://github.com/saadbuilt/mendwright/blob/main/SECURITY.md). A local model through Ollama keeps the page on your machine.
- If a page's cleaned HTML is longer than 60,000 characters, the rest is cut before it reaches the model. An element near the end is then visible to it only through the element inventory.
- How often a model picks the right element hasn't been measured, for any model.
- `--mendwright-strict` only fails the run that made the heal. Later runs pick up the saved heal and pass, which is why strict CI runs need a fresh store.

## Links

- [Guide](https://github.com/saadbuilt/mendwright/blob/main/docs/guide.md): configuration, the pytest plugin, CI recipes, the CLI and troubleshooting
- [Comparison](https://github.com/saadbuilt/mendwright/blob/main/docs/comparison.md)
- [Contributing](https://github.com/saadbuilt/mendwright/blob/main/CONTRIBUTING.md)
- [Security](https://github.com/saadbuilt/mendwright/blob/main/SECURITY.md)
- [Changelog](https://github.com/saadbuilt/mendwright/blob/main/CHANGELOG.md)

[MIT license](https://github.com/saadbuilt/mendwright/blob/main/LICENSE). If mendwright fixed a broken run for you, a star helps other people find it.
