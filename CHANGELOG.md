# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version starts with 0, a minor release can change the public API.

## [0.3.0] - Unreleased

First public release. Earlier versions were developed privately as selfheal and never published.

- Drivers for Playwright (sync and async, including patchright and camoufox) and Selenium, behind one `mendwright.open(page)`. `heal.locator(key, selector)` takes XPath, CSS or Playwright selectors. A heal is always an XPath.
- Providers for Anthropic, OpenAI and compatible APIs, Gemini, Bedrock, LiteLLM, Ollama and your own gateway. `auto_provider()` picks one from the environment, and there is no default model. `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL` are honoured for proxies and compatible hosts.
- Local checks before a heal is used: not an index-only path, not quarantined or superseded for the key, one match in the captured HTML, the step's role, no destructive word in a button or link label, and one visible match on the live page that still has the role and label. A heal whose action fails is quarantined, and the quarantine blocks the same element, not just the same selector string.
- Pay, buy, checkout, confirm, submit and delete controls are never healed. They are refused by key, by `text=`, and by the label of a button or link the model picked (visible text, `aria-label`, `title`, `alt` or an input button's `value`), unless the key has `destructive=False`.
- Page capture removes scripts, styles, comments and head metadata, and redacts values, URLs and long numbers, including in broken selectors and expected text. Direct providers have an hourly rate limit and an optional spend cap.
- A pytest plugin: the `heal` fixture, an overridable `mendwright_provider` fixture, and `--mendwright-strict`, which also fails reruns from pytest-rerunfailures. Heals from a passing test are confirmed.
- The `mendwright` command: `demo` (offline, or `--live` with the model from your environment), `doctor` and `report`.
- Heals are saved in `.mendwright/overrides.json`, with a journal next to it. A corrupt store raises instead of being overwritten. Each applied heal prints one line to stderr.
- Python 3.10 to 3.14, with lxml as the only dependency.

### Changed

From the private builds. If you never used them under the name selfheal, you can skip this.

- On Selenium, the handle for a candidate inside an iframe is a `FramedElement`. `driver.execute_script()` and `ActionChains` need `el.in_frame(lambda e: ...)` to get an element that still works in the frame (see the [guide](https://github.com/saadbuilt/mendwright/blob/main/docs/guide.md#selenium)).

[0.3.0]: https://github.com/saadbuilt/mendwright/releases/tag/v0.3.0
