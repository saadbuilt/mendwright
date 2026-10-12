# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version starts with 0, a minor release can change the public API.

## [Unreleased]

- mendwright's own local model for heals that never leave your machine. `LocalModelProvider` asks `mendwright-selector`, Qwen2.5-1.5B-Instruct fine-tuned with QLoRA, in Ollama: mendwright lists at most 40 elements of the step's role from the captured page (`mendwright.candidates`), the model answers which one the step meant or that none fits (removed, ambiguous, or moved to another page), and mendwright builds the XPath for the pick itself, never from its position, and checks it like any proposal. A pick the model is less than 80% sure of is declined (`MENDWRIGHT_LOCAL_MIN_CONFIDENCE` or `min_confidence=` changes that). When the checks refuse a pick because it is hidden or not unique on the live page, the model is asked once more without it; after any other refusal it doesn't guess again. `MENDWRIGHT_LOCAL_MODEL=<name>` or `MENDWRIGHT_PROVIDER=local` selects it, after cloud keys and the gateway and before `OLLAMA_MODEL`, and `mendwright doctor` shows it. On 3,000 held-out synthetic redesigns it picked the right element for 97.1% of broken selectors and a wrong one for 0.14%, and declined 97.1% of the steps whose element was gone; on the benchmark, behind the fingerprint tier, it healed 30 of 35 with no wrong heal. `training/` has the generator, the training and export scripts and the full results ([training/README.md](https://github.com/saadbuilt/mendwright/blob/main/training/README.md)).
- A provider that sets `wants_fingerprint = True` gets the key's saved fingerprint in the bundle, redacted like the expected text. Only `LocalModelProvider` does; hosted providers and gateways never get it.
- `benchmarks/run.py --provider local [--model NAME]`. For this provider the prompt size column counts the candidate task it sends.
- Flow changes (see the [guide](https://github.com/saadbuilt/mendwright/blob/main/docs/guide.md#flow-changes)):
  - `heal.interrupt(key, selector, action="click", args=(), role=None)` registers a step that can show up at any point, such as a cookie banner, a newsletter modal or an "are you still there?" dialog. mendwright handles it when it is in the way of a step (the step's element is missing, the page shows it before an action, or an action fails because something covered the element), then goes on with the step. A failed action is retried once, and only after an interruption was handled, never when Playwright's call log says the action was already done. At most 3 are handled per step. An interruption is never healed, and a later `heal.locator()` for its key is skipped when the element isn't there. When an action fails because its element was covered or couldn't be reached and no interruption matched, one WARNING names the ones mendwright looked for, since one whose selector no longer matches is the likeliest cause.
  - `mendwright.open(page, cookies="reject")` or `cookies="accept"` registers an interruption for the consent banners of OneTrust, Cookiebot, Didomi, TrustArc, Usercentrics, Quantcast Choice, Osano, cookieconsent, CookieYes and Complianz, with a fallback for other banners. The fallback acts only inside a short element that mentions cookies and holds no text field or navigation, so a consent screen that isn't about cookies (an OAuth Allow, a terms step) or a button such as Close account is never clicked. It is off by default.
  - `heal.locator(..., optional=True)` skips a step whose element isn't there instead of healing it. Proxies have `.present`, which is False for a skipped step, and its actions do nothing.
  - The prompt tells the model it may answer `{"xpath": null, "reason": "..."}` when no element on the page fits the step, and not to pick an element that is only similar. Providers report that as "the model says no element on this page fits: <reason>", and the reference gateway passes it on. The prompt also names the page path and the path where the key was last found.
  - All of this works the same in `mendwright.aio`, where actions awaited together look for interruptions one at a time.
  - New journal events: `flow.interrupt`, `flow.skip` and `flow.page_guard`.
- `mendwright demo` shows a cookie banner that now opens first and covers the form, handled as a registered interruption. `StandInPage` takes a `url=` and knows an overlay that blocks clicks until it is closed.
- An accuracy benchmark in `benchmarks/`: 47 cases where a redesign breaks a selector (12 of them where the right answer is no heal, 4 of those a step that moved to another page), a harness that runs them through `mendwright.open()` with any provider or a model-free baseline, and the baseline results. It is not part of the package. See [docs/benchmark.md](https://github.com/saadbuilt/mendwright/blob/main/docs/benchmark.md).

### Changed

- A key is no longer healed on a page other than the last few its element was found on. The store saves those pages (`lastPage` and `pages` in `overrides.json`, at most 5; numbers, uuids and other generated ids in a path count as one page), and such a heal is declined before any model call with an error that says the flow looks changed. Pass `heal_across_pages=True` to `mendwright.open()` to heal there anyway, for example after a redesign that only changed the URL. Pages loaded with `page.set_content()` (`about:blank`) are never compared.

## [0.3.0] - 2026-10-11

First public release. Earlier versions were developed privately as selfheal and never published.

- Drivers for Playwright (sync and async, including patchright and camoufox) and Selenium, behind one `mendwright.open(page)`. `heal.locator(key, selector)` takes XPath, CSS or Playwright selectors. A heal is always an XPath.
- Providers for Anthropic, OpenAI and compatible APIs, Gemini, Bedrock, LiteLLM, Ollama and your own gateway. `auto_provider()` picks one from the environment, and there is no default model. `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL` are honoured for proxies and compatible hosts.
- Local checks before a heal is used: not an index-only path, not quarantined or superseded for the key, one match in the captured HTML, the step's role, and one visible match on the live page that still has the role. A heal whose action fails is quarantined, and the quarantine blocks the same element, not just the same selector string.
- Keys you mark as destructive are never healed and never sent to a model: `destructive=True` in `heal.locator()`, `mendwright.open(destructive_keys=...)`, or `"destructive": true` in a seed file. mendwright doesn't guess this from the key or the page.
- Page capture removes scripts, styles, comments and head metadata, and redacts values, URLs and long numbers, including in broken selectors and expected text. Direct providers have an hourly rate limit and an optional spend cap.
- A pytest plugin: the `heal` fixture, an overridable `mendwright_provider` fixture, and `--mendwright-strict`, which also fails reruns from pytest-rerunfailures. Heals from a passing test are confirmed.
- The `mendwright` command: `demo` (offline, or `--live` with the model from your environment), `doctor` and `report`.
- Heals are saved in `.mendwright/overrides.json`, with a journal next to it. A corrupt store raises instead of being overwritten. Each applied heal prints one line to stderr.
- Python 3.10 to 3.14, with lxml as the only dependency.

### Changed

From the private builds. If you never used them under the name selfheal, you can skip this.

- On Selenium, the handle for a candidate inside an iframe is a `FramedElement`. `driver.execute_script()` and `ActionChains` need `el.in_frame(lambda e: ...)` to get an element that still works in the frame (see the [guide](https://github.com/saadbuilt/mendwright/blob/main/docs/guide.md#selenium)).

[Unreleased]: https://github.com/saadbuilt/mendwright/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/saadbuilt/mendwright/releases/tag/v0.3.0
