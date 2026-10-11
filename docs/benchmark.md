# Accuracy benchmark

When a redesign breaks a selector, how often does a heal land on the right element, and how often does mendwright accept a wrong one?
The benchmark in [`benchmarks/`](../benchmarks/) answers that for any provider mendwright supports, with a fixed set of cases you can rerun.

Results so far are in [benchmarks/RESULTS.md](../benchmarks/RESULTS.md).
Only the two model-free baselines have been run. No model numbers are published yet.

Contents:

- [What it measures](#what-it-measures)
- [The cases](#the-cases)
- [How a case runs](#how-a-case-runs)
- [Grading](#grading)
- [The baselines](#the-baselines)
- [Running it with your model](#running-it-with-your-model)
- [Cost of a run](#cost-of-a-run)
- [Caveats](#caveats)
- [Adding a case](#adding-a-case)

## What it measures

For each case the result is one of five outcomes:

| Outcome | Case | Meaning |
|---|---|---|
| correct | a heal is right | the healed selector reaches the right element |
| wrong | a heal is right | mendwright accepted a heal that reaches a different element |
| missed | a heal is right | no heal was applied |
| declined | the right answer is no heal | no heal was applied |
| wrong on negative | the right answer is no heal | mendwright accepted a heal anyway |

"Wrong" and "wrong on negative" are the numbers that matter most.
Both are heals that passed every local check, so a test would go on to click or fill an element the step never meant.
A miss costs a failed step and a person fixing the selector; a wrong heal can pass silently.

Each case also records:

- the healed selector, and every proposal with the gate's verdict and the element it reached
- how many wrong proposals the gate refused (for example a hidden duplicate, or a selector that matched two elements)
- model calls (a proposal, plus one retry when the gate rejects it)
- time spent in the provider's `propose()` call
- the prompt size in characters, from `mendwright.prompt.build_prompt()`, and an estimated token count (characters divided by 4)
- the reason when no heal was applied, and whether that reason was a failed call (an HTTP error, a bad key) rather than an answer

## The cases

43 cases, each in its own folder under `benchmarks/cases/`:

```text
benchmarks/cases/near-dup-cart-remove/
  case.json   id, category, description, key, selector, optional role, truth
  v1.html     the page the selector was written against
  v2.html     the same page after a redesign
```

`selector` is what the test code passes to `heal.locator()`. It matches exactly one element on v1 and nothing on v2.
`truth` is an XPath that matches exactly one element on v2, the one the step means, or `null` when the right answer is to not heal.

The pages are fictional sites written for the benchmark: an outdoor clothing shop, a project-management app and its marketing site, an online bank, a newspaper and its editor, a flight search site and a documentation site.
Each has a header, navigation, footer and, where it fits, a cookie banner, forms, tables, product grids and dialogs.
The v2 pages are what redesigns tend to produce: CSS-module and emotion class hashes, data-testid attributes, framework wrappers (`#__next`, `#root`, `fb-root`, `astro-island`), custom elements and Angular Material markup.
Pages are 3 KB to 14 KB.

| Category | Cases | What changed |
|---|---|---|
| id renamed or removed | 4 | the id the selector used is gone or renamed |
| hashed class churn | 3 | a new build changed the generated class names |
| wrapped | 3 | the element sits inside a new container or custom element |
| label reworded | 3 | the visible text the selector used changed |
| moved | 3 | the element moved to another part of the page |
| attribute churn | 3 | name, data-test or aria attributes changed |
| tag change | 3 | a div or link with role="button", or an input of type submit, became a button |
| near-duplicates | 4 | several elements look the same and only the row or card tells them apart |
| hidden duplicate | 3 | a hidden copy (mobile drawer, sticky bar, paywall dialog) sits next to the visible element |
| translated | 3 | the page is now in German, French or Spanish |
| form fields reordered | 3 | the selector depended on field order or numbered ids |
| removed (negative) | 3 | the element is gone; similar elements remain |
| ambiguous (negative) | 2 | the original is gone and several identical elements remain |
| removed near a dangerous control (negative) | 3 | a Save button is gone while Close account, Pay now or Publish is on the same page |

In every positive case the right element keeps the role mendwright learned on v1 and is visible.
Those are conditions mendwright's gate enforces on every heal, so a case that broke them would measure the gate rather than the model.
A link that became a real button, for example, is refused for its role whatever the model proposes.

No case marks its key `destructive=True`. mendwright refuses such keys before any model call, so they would only measure the refusal.

## How a case runs

`benchmarks/run.py` runs each case through the public API, the way a test suite meets a redesign:

1. `mendwright.open(page, provider=..., store_path=<temporary folder>)` on v1, then `heal.locator(key, selector)`. The selector matches, and mendwright learns the element's role.
2. The same page and Healer on v2 (loaded with `page.set_content()`), and `heal.locator(key, selector)` again. The selector misses, so mendwright captures and redacts the page, asks the provider, and runs every check it runs in a real test: the static checks against the captured HTML, then one match on the live page that is visible and still has the role. A rejected proposal gets one retry with the reason.
3. The harness grades whatever mendwright applied.

The page is `StandInPage` from `mendwright.demo` by default: an lxml page with the shape of a sync Playwright page, so no browser is needed.
`--browser chromium` runs the same cases in headless Chromium through Playwright instead.
For the similarity baseline, the two modes gave the same outcome and the same healed selector in all 43 cases.

Hourly heal limits are turned off for the run, `MENDWRIGHT_DISABLE` is set aside while the Healer opens, and each case gets a fresh store, so cases do not affect each other.
Nothing else in your environment is changed.

## Grading

Grading is by element identity, not by selector text.
After the heal, the harness parses the page as loaded (`page.content()`), runs both the healed selector and the case's truth XPath on it, and compares the lxml tree paths of the elements they reach.
`//button[@data-testid='promo-apply']` and `//form[@data-testid='promo-form']//button` count as the same answer when they reach the same button.

A healed selector that matches nothing or several elements in that document counts as wrong.

The tests in [`tests/test_benchmark.py`](../tests/test_benchmark.py) check the grader itself.
A proposer that returns the truth gets every positive case right and declines every negative case.
The `none` baseline misses every positive case and declines every negative case.
A proposer that picks another visible element with the right role is graded wrong, and one that proposes a hidden duplicate first is refused by the gate and counted.

## The baselines

`--provider none` never proposes anything. It shows the floor: no wrong heals and no correct ones.

`--provider similarity` works without a model, in the style of Healenium.
While the old selector still matches, it records the element: tag, attributes, text, label, the words around it and its ancestor path.
When mendwright asks for a heal, it scores every element of the expected role on the page mendwright sends, picks the most similar one, and builds an XPath for it from attributes, text or a nearby anchor.
Below a fixed score of 0.5 it declines.
On a retry it skips the element the gate just rejected.

It never sees the truth.
It does see the v1 page, which a model never gets: a model receives the key, the old selector and the redacted v2 page.
It is a short reimplementation of the idea for this benchmark, not Healenium itself, and its numbers say nothing about how Healenium would score.

On the 43 cases it healed 27 of 35 positive cases correctly, missed 7, and healed 1 onto the wrong element.
It declined 6 of the 8 negative cases and healed the other 2 onto an element the step did not mean.
Every one of those 3 wrong heals passed mendwright's checks: the element was unique, visible and of the right role.
The checks guard against brittle, ambiguous and hidden targets. They cannot tell whether a plausible element is the one the step meant.

## Running it with your model

Set up the provider the way you would for `mendwright.open()` (see [the guide](guide.md#providers)), then:

```bash
python benchmarks/run.py --provider anthropic --model <model id>
python benchmarks/run.py --provider openai --model <model id>
python benchmarks/run.py --provider gemini --model <model id>
python benchmarks/run.py --provider ollama --model <model name>
python benchmarks/run.py --provider auto          # whatever mendwright.auto_provider() picks
python benchmarks/report.py                        # rebuild benchmarks/RESULTS.md
```

Run it from the repository root, with mendwright installed (`pip install -e .` in a clone).
Before the first case it prints the provider, the model and the endpoint the pages are sent to, and says when a variable such as `ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL` is set, so a proxy in your environment is visible before anything is sent.
It never prints or stores a key.

Options:

| Option | Meaning |
|---|---|
| `--model ID` | the model id. Without it, the provider's `*_MODEL` variable is used. |
| `--cases GLOB` | run only the cases whose id matches, for example `"near-dup-*"` |
| `--repeat N` | run every case N times. Repeats count as separate attempts in the totals. |
| `--browser chromium` | use headless Playwright instead of StandInPage (also `firefox` or `webkit`) |
| `--out FILE` | where to write the results. The default is `benchmarks/results/<provider>-<model>-<page>-<date>.json`. |

The results file holds the date, mendwright and Python versions, the provider class, model id and endpoint, the page mode, a hash of the cases, a summary and one record per case.
A markdown summary row is printed at the end.
If any case was declined because a call failed (a rate limit, a network error), the summary says so; rerun before comparing numbers.

The pages are fictional, so sending them to a hosted model exposes nothing about a real site.

## Cost of a run

The prompt mendwright sends is the redacted page HTML plus an inventory of its interactive elements.
Measured on the 43 cases, one prompt is 3,514 to 19,385 characters, 9,761 on average, which is about 2,400 tokens at 4 characters per token.

One pass makes 43 to 86 model calls: one per case, plus a retry for each proposal the gate rejects.
That is about 105,000 input tokens when every first proposal passes or is declined, and up to about 210,000 when every case needs a retry.
Replies are a short JSON object; at 100 to 200 tokens each, output is about 4,000 to 17,000 tokens.

To price a run, multiply by your provider's current rates:

```text
cost = input_tokens / 1,000,000 * input_price + output_tokens / 1,000,000 * output_price
```

For example, at $3 per million input tokens and $15 per million output tokens, one pass costs roughly $0.40 to $0.90.
Those rates are an example to show the arithmetic, not a quote for any model. `--repeat 3` triples the cost.
HTML often comes out at fewer than 4 characters per token, so the real token count can be higher than this estimate; your provider's usage page has the exact figure.

## Caveats

- The corpus is small and written for the benchmark. 43 hand-built cases on fictional sites are not a sample of real redesigns, and each case moves a percentage by about 3 points.
- The categories cover failure modes that are known to break selectors. How often each happens in practice is not modelled.
- A single run varies. The built-in Anthropic, OpenAI-compatible and Gemini providers do not set a temperature, so the provider's default applies and two runs can disagree. Use `--repeat` to see how much.
- The model and the date matter. A results file records the model id, the mendwright version, the date and a hash of the cases; compare runs that match on those.
- The similarity baseline sees the old page and a model does not, so the two are not on equal terms.
- mendwright's prompt asks for a JSON object with an XPath. It does not tell the model that "the element is gone" is an acceptable answer, so on the negative cases a model has to return no XPath on its own initiative, or mendwright's checks have to stop it.
- The pages are static. There is no single-page-app re-rendering, no iframes, no shadow DOM and no timing.
- StandInPage only knows inline visibility (`display:none`, `visibility:hidden`, the `hidden` attribute). The cases hide things that way, so both page modes agree.

## Adding a case

Make a folder under `benchmarks/cases/` with `case.json`, `v1.html` and `v2.html`, then run:

```bash
python -m pytest tests/test_benchmark.py -q
python benchmarks/run.py --provider similarity
python benchmarks/run.py --provider none
python benchmarks/report.py
```

`tests/test_benchmark.py` enforces the rules for a case:

- the selector is an XPath or an `#id` CSS selector, and matches one element on v1 and none on v2
- the truth matches one element on v2, or is `null` for a negative case
- the right element has the role the v1 element has, and is not hidden
- each page is under 25 KB, writes `<tbody>` in tables, loads nothing from the network, and hides things with inline styles or the `hidden` attribute
- the published baseline runs were made on the current cases (the hash in the results file matches), and `RESULTS.md` matches the results files
