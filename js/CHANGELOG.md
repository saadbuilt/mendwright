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
- `cookies: 'reject'` and `cookies: 'accept'`, and `heal.interrupt()`.
- The store is the Python package's `.mendwright/overrides.json` (schema 2) with its lock file and merge, and the journal is its `journal.jsonl`, so one store can be shared by both packages. It keeps the `shadowHosts` the Python package 0.10.0 saves for a key and a heal inside an open shadow root, merges them as it merges `frameXPath`, and, as the Python package does, drops a heal's `shadowHosts` when your code passes that heal's chain.

[0.1.0]: https://github.com/saadbuilt/mendwright/tree/main/js
