# Contributing to mendwright

mendwright has one runtime dependency (lxml), and the core runs without a browser library or a vendor SDK installed.
Changes that keep it that way are the easiest to merge.
New drivers and new providers are the most useful contributions; their contracts are below.

## Where to start

- Pick an issue labelled [good first issue](https://github.com/saadbuilt/mendwright/labels/good%20first%20issue).
- Report a heal that picked the wrong element, with the "A heal picked the wrong element" issue template. These reports are the only accuracy data the project has.
- Add a driver for another browser library ([Writing a driver](#writing-a-driver)) or a provider for another model API ([Writing a provider](#writing-a-provider)).

## Setup

Python 3.10 or later.

```bash
git clone https://github.com/saadbuilt/mendwright
cd mendwright
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

`[dev]` installs pytest, ruff, build and twine.
Keep the quotes around `".[dev]"`; zsh treats the brackets as a glob otherwise.

For the real-browser tests, also install the browser libraries and Playwright's browsers:

```bash
pip install -e ".[dev,playwright,selenium]"
python -m playwright install chromium firefox webkit
```

Selenium drives the Chrome and Firefox already installed on your machine.
Selenium Manager downloads a matching driver the first time.
patchright and camoufox are optional: `pip install patchright` then `python -m patchright install chromium`, and `pip install camoufox` then `python -m camoufox fetch`.

## Running the tests

### Offline suite

```bash
python -m pytest
```

This needs no network, API key or browser, and takes about 10 seconds.
One or two tests skip on purpose: one needs a live gateway, and a POSIX-only one skips on Windows.

`pyproject.toml` sets `addopts = "-m 'not browser' -p no:mendwright"`.
The first part leaves out the browser tests.
The second turns off mendwright's own pytest plugin, because this repo's tests call `mendwright.open()` hundreds of times and the plugin would print a heal summary at the end. `tests/test_pytest_plugin.py` loads the plugin explicitly through pytester.

New tests must not reach the network, a real model or a real browser, and must not depend on the clock or on randomness.
The fixtures in `tests/conftest.py` cover most cases:

- `make_page`: a `FakePage`, an lxml-backed page shaped like a Playwright page.
- `stub_provider`: a `StubProvider` that returns a fixed proposal and records what it was sent.
- Autouse fixtures that unset `MENDWRIGHT_STORE`, `MENDWRIGHT_TIMEOUT_MS`, `MENDWRIGHT_DRY_RUN` and `MENDWRIGHT_DISABLE`, and reset the shared rate limiters.

Providers take an injected `http=` transport; `tests/test_providers.py` shows the pattern.

`tests/test_lists_emptied.py` holds the acceptance matrix of list lookups whose selectors match nothing: emptied lists (`REFUSE`) and redesigns (`REDESIGN`). It must keep passing as it is, for calls without `nonempty=True`. `tests/test_lists_nonempty_matrix.py` runs the same matrix with `nonempty=True`: every case must raise and return no element, and its outcome table (`REFUSE_PROPOSED`, `REFUSE_STATE`, `REDESIGN_REFUSED`, with `L11`, `L15`, `L18c` and `L20` for the codes of the guide's refusals) says which cases are proposed and which are refused. What must hold whatever you change: no redesign is proposed onto the other list, at least 21 redesigns are proposed, and every emptied list that is proposed says its heading or container is not the same. If a change moves another case, update the table in the same commit and say why in its message. Its golden files in `tests/fixtures/lists_080` are what 0.8.0's code wrote and printed for the same lookups and reports: to make them again, run `scenario_080()` and `report_080()` of that file with 0.8.0's `mendwright` and `tests` first on `sys.path`.

### Browser suite

```bash
python -m pytest -m browser
```

A `-m` on the command line replaces the default one.
The suite is in `tests/browser/`:

- The sync tests run on 7 engines: `pw-chromium`, `pw-firefox`, `pw-webkit`, `patchright`, `camoufox`, `sel-chrome` and `sel-firefox`.
- `test_aio_browser.py` runs `mendwright.aio` on Chromium, Firefox and WebKit.
- An engine that isn't installed is skipped, and the skip message names the install command.

Run one engine with `-k`, for example `python -m pytest -m browser -k pw-webkit`.
Run one scenario on every engine with, for example, `-k heal_after_a_redesign`.
The last full local run passed on all 7 engines, plus the async tests on all 3 browsers (Windows 11, Python 3.12). The sync suite took a little over 2 minutes.

`MENDWRIGHT_REQUIRE_ENGINES` takes a comma-separated list of engine ids that must start.
If one of them fails to launch, its tests fail instead of being skipped. CI sets it to `pw-chromium,pw-firefox,pw-webkit,sel-chrome,sel-firefox`.
Only the fixture in `tests/browser/conftest.py` reads it. `test_aio_browser.py` has its own `engine` fixture, which still skips an engine that can't launch.

Rules for browser tests:

- Mark them with `pytestmark = pytest.mark.browser`.
- Do not import playwright or selenium at module level or in a conftest. The default run still collects `tests/browser/` in CI jobs that have neither installed. Use `pytest.importorskip()` or import inside the test.
- A test that only applies to one family takes `@pytest.mark.engine_family('playwright')` or `('selenium')`. It is deselected on the other family, so the skip count only shows real gaps.

### Gateway parity test

`tests/test_gateway_worker.py` runs `examples/heal_gateway.mjs` under node, with `fetch` stubbed and no network.
It checks that the worker's `buildPrompt()` and `parseProposal()` give the same results as `build_prompt()` and `parse_proposal()` in `mendwright/prompt.py`, and that the worker reads its model id from `MODEL`.
It's skipped when `node` isn't on PATH.

If you change `mendwright/prompt.py`, make the same change in `examples/heal_gateway.mjs` and run:

```bash
python -m pytest tests/test_prompt.py tests/test_gateway_worker.py
```

## Lint

```bash
ruff check .
```

The configuration is in `pyproject.toml`: rules E, F, W and B, line length 112.

## Architecture

What happens in one call to `heal.locator(key, selector).click()`:

```
heal.locator(key, selector)               facade.py (sync), aio.py (async)
  resolve()                               resolver.py, through a Driver (drivers/)
  on a miss: HealCoordinator.try_heal()   coordinator.py
    destructive, guard, role, rate checks
    build_bundle()                        capture.py
    provider.propose(bundle)              providers/, prompt.py
    apply(): validate(), live_match()     client.py, validation.py, gate.py
    save the new candidate                store.py
  resolve() again
.click()                                  ActionProxy in facade.py, through driver.act_on()
  on failure: quarantine the heal         coordinator.confirm_pending()
```

The modules:

- `selectors.py`: `classify()` (XPath, CSS, Playwright engine selector or `get_by_*` call), `check_syntax()`, `normalize()` and `InvalidSelector`.
- `getby.py`: the keys of `heal.page.get_by_*()` calls (`mendwright.open(heal_get_by=True)`). `canonical()` writes a call as its key, `parse()` reads one back without running it, `call_of()` says which calls are never keys, `get_by_key()` is the public builder, and `not_found_error()` makes the miss that is also Playwright's `TimeoutError`.
- `drivers/`: `base.py` holds the `Driver` protocol, the shared describe and inventory scripts, and the fallback helpers. `registry.py` has `wrap()` and `register_driver()`. The built-in drivers are `playwright_sync.py`, `playwright_async.py` (used by `mendwright.aio`) and `selenium.py`, which handles frames, implicit waits and the verb mapping.
- `fingerprint.py`: the fingerprint tier. `FINGERPRINT_JS` reads an element in one round trip and `from_raw()` normalizes it; `find()` scores the elements of a captured page against a stored fingerprint and returns a unique XPath only for a confident match.
- `lists.py`: list fingerprints. `LIST_JS` reads the items of a list in one round trip and `from_raw()` normalizes what it gives; `LIST_PLACE_JS` also reads its heading and where it sits, for `nonempty=True`. A list is never healed: `find()` looks for a list whose selectors match nothing, and what it finds is only proposed (`HealCoordinator.propose_list()`, `client.list_gate()`, `SelectorStore.propose_list_heal()`), for a person to accept with `mendwright lists`.
- `values.py`: `expect_value=`. The named checks, `parse_number()` and `parse_money()`, and the text of a refused read.
- `proxies.py`: `heal.page` and `heal.driver`, the page and driver stand-ins that send selector lookups through `heal.locator()` and everything else to the real object.
- `resolver.py`: `resolve()` and `present()`. Tries the candidates in order and polls until the timeout, then raises `AttentionRequired`, or `InvalidSelector` when no candidate can run. `resolve_get_by()` and `present_get_by()` do the same for a call key: the call first, its saved heals only while it matches nothing.
- `facade.py`: `mendwright.open()`, `Healer` and `ActionProxy`. Puts the selectors from your code first, reads the role from the page, never acts through a saved heal on a key marked destructive, and runs actions, quarantining a heal whose action fails. It also handles interruptions, optional steps and the page guard.
- `flow.py` and `interruptions.py`: page paths, interception errors and the texts for flow changes; `heal.interrupt()`'s checks and the selectors of the `cookies=` preset.
- `aio.py`: `mendwright.aio.open()` and `AsyncHealer`. A heal runs the sync coordinator in a worker thread, which reaches the page through `SyncBridge`.
- `drift.py` and `drift_session.py`: `heal.drift()` and `mendwright check`. `probe_page()` looks at each key's saved selectors as a run does, the call first for the key of a `get_by_*` call, with `count()`, `is_visible()` and `describe()` only, and previews a heal with the run's own `try_heal()` on a `SelectorStore.snapshot()`. `drift_session.py` holds the headless Playwright browser `mendwright check` loads pages in.
- `coordinator.py`: `HealCoordinator` and `resolve_with_heal()`. Runs the checks before any heal, tries the fingerprint tier, builds the bundle, asks the provider (with one re-prompt on rejection), applies, journals, and keeps the reason for `explain()`.
- `validation.py` and `gate.py`: `validate()` checks a proposal against the captured HTML with lxml; `live_match()` checks it against the page through the driver. `client.apply()` runs both.
- `store.py` and `roles.py`: the seed and override layers, candidate status, per-key metadata, the lock file and the merge; and the role vocabulary.
- `capture.py`: `build_bundle()`, `redact()` and `clean_html()`.
- `providers/`: the `HealProvider` protocol (`base.py`), the built-in providers, `auto_provider()` (`detect.py`) and the error text (`_errors.py`). `prompt.py` holds the one prompt every provider uses.
- `limits.py` and `journal.py`: the rate limit and spend cap for direct providers, and the JSONL journal.
- `publish.py`: writes a confirmed selector into a seed JSON file.
- `cli.py` and `demo/`: the `mendwright` command and the demo that ships in the wheel. `fixback.py` is `mendwright fix`, its JSON and its Markdown, `report_html.py` is `mendwright report --format html`, and `_md.py` puts text from a store or a journal into Markdown, in code spans and code blocks.
- `action.yml` and `action/` (not part of the package): the GitHub Action. `action/mendwright_action.py` holds its steps, standard library only, and `action/requirements.txt` the one hash-pinned lxml wheel it installs. `tests/test_action_script.py` runs the steps against a real git repository and a fake GitHub API.
- `pytest_plugin.py`: the `heal` fixture, confirmation from test results, strict mode and the summary.

## Writing a driver

A driver wraps one live page or session.
`wrap()` accepts any object that has `count`, `is_visible` and `handle`; anything else goes through the registered detectors.

| Method | Status | Contract |
|---|---|---|
| `count(selector, frame_chain=())` | required | Number of matches. |
| `is_visible(selector, frame_chain=())` | required | Whether the match is visible. |
| `handle(selector, frame_chain=())` | required | The native locator or element, so user code can act on it. |
| `scroll_into_view(selector, frame_chain=())` | expected | |
| `act(selector, verb, *args, frame_chain=())` | expected | Perform a verb on the match. |
| `url()` | expected | The current URL. |
| `outer_html(scope=None)` | expected | The HTML the checks run against. |
| `act_on(native, verb, *args, **kwargs)` | optional | Run a Playwright-style verb on a `handle()` result. Without it, `driver_act_on()` calls `getattr(native, verb)(*args, **kwargs)`. |
| `describe(selector, frame_chain=())` | optional | `{'tag', 'role', 'text', 'outer_html'}` for a single match, else `None`. Without it, `driver_describe()` returns `None` and the role is guessed from the selector text. |
| `frame_matches(selector)` | optional | The iframes of the page in which the selector matches, as `[{'name', 'url', 'count', 'nested', 'selector'}]`; `selector` is a CSS selector for the iframe element (see `frame_selector()` in `mendwright/drivers/base.py`), or `None`. Without it, mendwright doesn't look into iframes on its own. |
| `framed(frame_selector)` | optional | A driver for the document inside the one iframe `frame_selector` matches, or `None`. Without it, a key found inside an iframe heals on the page. |
| `fingerprint(selector, frame_chain=())` | optional | The fingerprint (see `mendwright/fingerprint.py`) of the one element the selector matches, else `None`. Run `FINGERPRINT_JS` on the match and pass the result through `from_raw()`. One that takes `hosts=True` also says where the element is: pass the result through `shadow.with_hosts()` then, which adds the shadow hosts around it as `shadowHosts`. Without it, `driver_fingerprint()` returns `None` and the fingerprint tier never has anything to look for. |
| `inventory(limit=200)` | optional | Up to `limit` rows of `{'tag', 'role', 'id', 'name', 'text', 'aria_label'}` for buttons, links, inputs, selects, textareas and `[role]` elements. Without it, `driver_inventory()` returns `[]`. |
| `visible_match(selector, frame_chain=())` | optional | `describe()`'s result for the first visible element of those the selector matches, plus `'visible'`: how many of the matches are visible, counted up to 2. `None` when none is. The WARNING for an interruption or a landmark whose selector matches several elements builds the selectors it suggests from it when exactly one is visible. Without it, or without `'visible'`, the WARNING suggests none. |
| `handles(selector, frame_chain=())` | optional | A native handle for each element the selector matches, for a list. Without it, `driver_handles()` gives `[handle(selector)]`. |
| `list_fingerprint(selector, frame_chain=(), *, place=False)` | optional | The list fingerprint (see `mendwright/lists.py`) of the elements the selector matches, else `None` for fewer than two. Run `LIST_JS` on the matches, or `LIST_PLACE_JS` with `place=True`, and pass the result through `lists.from_raw()`. Without it, no list is recorded, so a list lookup that matches nothing gives `[]` at once. One without `place=` is called as before, and `nonempty=True` can't look for its lists. |
| `list_items(selector, frame_chain=())` | optional | `[{'tag', 'role', 'type', 'visible'}]` for the first 200 elements the selector matches: run `ITEMS_JS` on them and pass the result through `item_rows()` in `drivers/base.py`. Raise on any error. Without it, `nonempty=True` can't check a list it finds. |
| `shadow_html(hosts)` | optional | The HTML inside the open shadow root that `hosts` (CSS selectors, outermost first) lead to, or `None` when they lead to none: run `SHADOW_HTML_JS` from `drivers/base.py`. A driver with it takes `ShadowHost` steps (see `mendwright/shadow.py`) at the end of a frame chain in every method: the selector is then CSS, and must match only inside that shadow root, never in the host's light DOM or a shadow root inside it (Playwright's CSS pierces, so its drivers check `PLACE_JS`). Without it, no heal runs inside a shadow root, and a candidate saved inside one can't run. |
| `shadow_inventory(hosts, limit=200)` | optional | `inventory()` of that shadow root (`SHADOW_INVENTORY_JS`), `[]` when there is none. |
| `shadow_matches(selector)` | optional | `(roots, cut)`: the open shadow roots a CSS selector matches in, or the HTML of each one for an XPath (`SHADOW_ROOTS_JS`, then `shadow.roots_of()`). Without it, mendwright doesn't follow a selector into a shadow root. |
| `role_match(selector, role, level=None, frame_chain=())` | optional | `True` when the browser gives the one element the selector matches the ARIA role `role` (and heading level `level`), `False` when it doesn't, `None` when it can't tell. The built-in Playwright drivers ask `get_by_role(role, level=level).and_(the selector's locator).count() == 1`. The live check of a heal of a `get_by_role()` call key needs it: without it, `driver_role_match()` returns `None` and such a heal is refused. |

Rules:

- `frame_chain` is a tuple of frame selectors, outermost first, then, for a driver with `shadow_html()`, the `ShadowHost` steps of the shadow hosts, outermost first.
- A selector can be XPath, CSS, a Playwright engine selector or a `get_by_*` call, the kind `get_by` (see `mendwright/selectors.py` and `mendwright/getby.py`). Raise `InvalidSelector` for a kind your library can't run, or one the browser rejects; check it with `check_syntax(selector, calls=True)`, which returns the kind. For any other error, the read methods return `0`, `False` or `None`, so the resolver keeps polling.
- `outer_html(scope)` must capture the same document or frame that resolution runs against. Otherwise a selector can be unique in the snapshot and not on the page.
- `describe()` must compute `role` and `text` the way `validate()` does: `validation._role_of()` on the element, and its whitespace-normalised text, cut to 200 characters. The built-in drivers do this with `DESCRIBE_JS` and `summary_from_js()` in `drivers/base.py`.
- `inventory()` must not read input values.
- Detect your page by its methods, not its module name. camoufox and patchright pages aren't in the `playwright` module, and a wrapper has no stable module name. Register with `register_driver(detect, make)`, or `register_driver_front(detect, make)` to be checked before the built-in drivers.
- For `mendwright.aio`, a custom driver whose `count`, `is_visible` and `handle` are coroutine functions is accepted by `mendwright.aio.open()`.

```python
from mendwright import register_driver

register_driver(my_detect, MyDriver)   # my_detect(obj) -> bool, MyDriver(obj) -> driver
```

The tests to copy from are `tests/test_driver_protocol.py`, `tests/test_driver_describe_inventory.py` and `tests/test_driver_act_on.py`.

## Writing a provider

A provider turns a capture bundle into a proposal:

```
propose(bundle) -> {'xpath': str, 'fallbacks': [str, ...], 'reasoning': str} | None
report_outcome(key, xpath, success) -> None
last_error: str | None
```

`HealProvider` in `mendwright/providers/base.py` is the protocol.
`last_error` is optional for providers outside this repo (the coordinator reads it with `getattr`), and required for built-in ones.
Two optional class attributes change what the coordinator sends: `wants_fingerprint = True` adds the key's saved fingerprint to the bundle (only for a model on the user's machine), and `wants_full_html = True` sends the page as captured instead of fitted to the 60,000-character prompt (`mendwright/fit.py`), which also lets a proposal use a position such as `[2]`. A provider that builds its prompt with `build_prompt()` leaves both off.

Rules:

- Build the request with `mendwright.prompt.build_prompt(bundle)` and parse the reply with `parse_proposal(text)`. Do not write a second prompt.
- Set `last_error = None` in `__init__` and at the start of `propose()`. When `propose()` returns `None`, set it to one or two sentences: what happened, then a next step that names a real setting. It must not contain the API key, an auth header, the request body or a URL query string. `Describer` in `mendwright/providers/_errors.py` does this for the built-in providers.
- Import the vendor SDK inside the method that needs it, with a stdlib `urllib` fallback, so `import mendwright` works without the SDK. Bedrock is the exception: its requests are SigV4-signed, so it needs boto3, and only constructing a `BedrockProvider` without it raises.
- Never hardcode a model id. Take it from a `model=` argument or a `<PROVIDER>_MODEL` environment variable, and raise an error that names the variable when neither is set.
- Accept an `http=` transport so tests can run offline.
- A provider only proposes. It never writes to the store or touches the page; the coordinator checks every proposal locally before it is used.

The tests to copy from are `tests/test_provider_last_error.py` and the `tests/test_provider_*.py` file for a similar provider.

## Pull requests

Before you open one, check:

- [ ] `python -m pytest` passes, and new behaviour has offline tests.
- [ ] `ruff check .` passes.
- [ ] No new required dependency. Vendor SDKs stay optional and are imported lazily.
- [ ] No model id in library code.
- [ ] The checks aren't weakened: a provider only proposes, and `validate()`, `live_match()`, the destructive-control refusal and `guard` still apply.
- [ ] If `mendwright/prompt.py` changed, `examples/heal_gateway.mjs` changed to match, and `tests/test_gateway_worker.py` passes with node installed.
- [ ] If the change is user-visible, `CHANGELOG.md` has an entry under the next version.
- [ ] If a driver changed, `python -m pytest -m browser` passes for the engines you have.

Keep pull requests small and about one thing.

## Releases

See [docs/RELEASING.md](docs/RELEASING.md).

## Reference

### Live gateway test

`tests/test_success_heal.py` has one test against a deployed gateway. It runs only when `MENDWRIGHT_GATEWAY` is set:

```bash
export MENDWRIGHT_GATEWAY=https://heal.example.workers.dev
export MENDWRIGHT_KEY=<your client key>
python -m pytest tests/test_success_heal.py
```

### What CI runs

`.github/workflows/ci.yml` has these jobs:

| Job | What it runs |
|---|---|
| lint | `ruff check .` on Python 3.12. Findings fail the job. |
| test | `python -m pytest -q` on Ubuntu with Python 3.10, 3.11, 3.12, 3.13 and 3.14, on Windows with 3.10 and 3.14, and on macOS with 3.12. |
| test-next | The same on the Python 3.15 prerelease. Failures are reported but don't fail the run. |
| lowest-deps | The offline suite on Python 3.10 with lxml 5.0.0 and pytest 7. |
| browsers | `python -m pytest -m browser -q -rs` on Ubuntu with Python 3.12. It installs Playwright's three browsers, uses the runner image's Chrome and Firefox for Selenium, and sets `MENDWRIGHT_REQUIRE_ENGINES`. patchright and camoufox aren't installed there, so their tests skip. |
| package | Builds the sdist and wheel, runs `twine check --strict`, installs the wheel into a fresh venv, runs `mendwright --version`, `python -m mendwright doctor` and `mendwright demo`, then runs the sdist's tests against the installed wheel. |

### Charts

The comparison charts in `docs/assets/` are generated from `docs/charts/data.json`:

```bash
python docs/charts/build_charts.py           # rewrite the 12 SVGs
python docs/charts/build_charts.py --check   # exit 1 if an SVG is out of date, 2 if data.json is invalid
```

The script uses only the standard library and gives the same bytes on every run.
`docs/assets/demo.svg`, a terminal recording of `mendwright demo` (no longer in the README), is built from the real output of `mendwright demo` with `python docs/charts/build_demo_svg.py` (also `--check`). Run it again after any change to the demo's output.
CI doesn't run either one, so run `--check` before a pull request that touches `data.json` or the demo.
