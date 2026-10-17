# Changelog

All notable changes to the npm package `mendwright` are documented here. The Python package has [its own changelog](https://github.com/saadbuilt/mendwright/blob/main/CHANGELOG.md).

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version starts with 0, a minor release can change the public API.

## [0.1.0] - Unreleased

The first release: the Python package's `heal.locator()` and `heal.page` for Playwright Test.

- `import { test, expect } from 'mendwright/test'` makes the `page` fixture heal: `page.locator(selector)` and the page methods that take a selector heal when the selector broke, with the selector as the key. The `heal` fixture is the healer, and the heals of a test are confirmed when it passes. `mendwrightStrict` fails a test that passed only because of a heal.
- `open(page)` and `heal.locator(key, selector, { role, destructive, optional, intent })` for a page you opened yourself.
- The fingerprint tier, with the Python package's scoring, thresholds and text rules, before any model call.
- Anthropic, OpenAI and compatible hosts, and Ollama (any model, or mendwright's own selector model) as providers, picked from the same environment variables as the Python package.
- The gate: an XPath is used only when it isn't an index-only path, isn't quarantined or superseded and doesn't reach a quarantined element, and matches exactly one element of the key's role in the captured page and one visible element of that role on the live page.
- Keys marked destructive, keys on a page they were never found on, keys whose role is unknown and steps while a guard blocks are never healed. A heal whose action fails in the browser is quarantined.
- Open shadow roots: the fingerprint read saves where the element is as the key's `shadowHosts`, as the Python package 0.10.0 does, and a key last found inside a shadow root is never healed, so a look-alike on the page around its host is never picked. A heal the Python package saved inside a shadow root runs strictly there: each host must match one element and have an open shadow root, and only a match inside it counts. The fingerprint reads a form field's `<label for=...>` in the field's own shadow root, as the Python package does.
- `cookies: 'reject'` and `cookies: 'accept'`, and `heal.interrupt()`.
- The store is the Python package's `.mendwright/overrides.json` (schema 2) with its lock file and merge, and the journal is its `journal.jsonl`, so one store can be shared by both packages. Each journal line has the run id the Python package 0.10.0 gives it (`MENDWRIGHT_RUN_ID`, or `gh-<run id>.<attempt>` on GitHub Actions), and a heal that did not apply is a `heal.declined` with its code (for a key last found inside a shadow root or an iframe, `dry_run` and `disabled` in dry run and under `MENDWRIGHT_DISABLE=1`, as the Python package gives them) and an interruption that kept coming back a `flow.stop`, so the Python package's `mendwright notify` sums up a run of this package too. It keeps the `shadowHosts` the Python package 0.10.0 saves for a key and a heal inside an open shadow root, merges them as it merges `frameXPath`, and, as the Python package does, drops a heal's `shadowHosts` when your code passes that heal's chain.

[0.1.0]: https://github.com/saadbuilt/mendwright/tree/main/js
