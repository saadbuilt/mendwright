# Demo

Two small offline demos of the same idea: a redesign breaks a selector, mendwright gets a new one from a provider, checks it against the page, and retries the step.
Neither needs a browser or an API key.

- `mendwright demo` ships in the package and runs anywhere mendwright is installed. It uses a checkout page.
- `python examples/demo/demo.py` is the same idea as a short script you can read and copy. It uses a signup page.

## mendwright demo

```bash
pip install mendwright
mendwright demo
```

Output from a run:

```
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

The pages are `mendwright/demo/v1.html` and `v2.html`, a checkout form.
In v2 the ids `accept-terms` and `pay` are gone, while the checkbox, its label and the "Pay now" button are unchanged.
The checkbox is healed. The demo marks the pay step `destructive=True`, so the pay button is refused without a model call (see [Destructive controls](../../SECURITY.md#destructive-controls)).

Options:

- `--browser [chromium|firefox|webkit]` runs the same demo in a real headless Playwright browser. With no value it uses chromium. It needs `pip install "mendwright[playwright]"` and `python -m playwright install chromium` (or the browser you name).
- `--live` asks the model from your environment (for example `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`) instead of the scripted reply. Only the demo page is sent. The pay button is still refused without a model call.
- `--keep` keeps the store from the run and prints its path, so you can look at it with `mendwright report --store <path>`.
- `--slow` pauses 0.4 seconds between lines, for screen recordings.

The demo writes only to a temporary folder and deletes it at the end, unless you pass `--keep`.
It exits with 1 if the checkbox isn't healed or the pay button isn't refused.

## demo.py

From a clone:

```bash
git clone https://github.com/saadbuilt/mendwright
cd mendwright
pip install -e .
python examples/demo/demo.py
```

`demo.py` imports `mendwright.demo`, which ships in the package, so any mendwright 0.3.0 install works (`pip install mendwright` or `pip install -e .`). From an uninstalled clone, run it from the repo root with `PYTHONPATH=.`.

Output from a run:

```
v1  #accept-terms matched, box checked
v2  #accept-terms no longer matches
    healed: #accept-terms -> //label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox'] (confirmed)
result: red -> green
```

| File | What it is |
|---|---|
| `examples/demo/v1.html` | A signup form (not the checkout page `mendwright demo` uses). The terms checkbox has `id="accept-terms"`. |
| `examples/demo/v2.html` | The same signup form after a redesign. The checkbox now sits inside a `<ds-checkbox>` element and has a generated id. Its type and its label text are unchanged. |
| `demo.py` | Opens a `Healer`, checks the box on v1, shows the selector missing on v2, then calls the same `heal.locator()` line again, which heals and checks the box. |
| [`tests/test_demo.py`](../../tests/test_demo.py) | Runs `demo.main()` and checks the heal, its confirmation, the saved store and the printed lines. |

## How it works

Both demos use two stand-ins from `mendwright.demo`. Everything else is the code a real run uses.

- `StandInPage` is an lxml page with the methods of a sync Playwright page. `mendwright.open()` detects it as Playwright, so the real driver, resolver, checks and store run against it. `#accept-terms` misses on v2 because v2 has no element with that id, and the healed XPath is checked against the same v2 DOM.
- `ScriptedProvider` builds the real prompt with `build_prompt()` and parses a canned reply with `parse_proposal()`. It makes no API call. The model reply is the only thing that is canned.

`box.check(expect=box.is_checked)` confirms the heal only if the box really is checked afterwards. If `expect=` returns false, the healed selector is quarantined.

## With a real browser and model

Swap the stand-ins for a Playwright page, and leave `provider=` out so `mendwright.open()` picks a provider from the environment (for example `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`):

```python
from pathlib import Path

from playwright.sync_api import sync_playwright

import mendwright

HERE = Path('examples/demo')

with sync_playwright() as p:
    page = p.chromium.launch().new_page()
    heal = mendwright.open(page, timeout_ms=2000)

    page.goto((HERE / 'v1.html').resolve().as_uri())
    heal.locator('accept_terms', '#accept-terms').check()

    page.goto((HERE / 'v2.html').resolve().as_uri())
    box = heal.locator('accept_terms', '#accept-terms')
    box.check(expect=box.is_checked)
    print(heal.heals)
```

This sends a redacted copy of v2.html to your provider. [SECURITY.md](../../SECURITY.md#redaction) lists what redaction removes.
The heal is saved to `./.mendwright/overrides.json`, so a second run uses it without calling the model.
This snippet has been run in headless Chromium with `ScriptedProvider` standing in for the model, but not yet against a real model. `mendwright demo --live` is the quickest way to try your own.
