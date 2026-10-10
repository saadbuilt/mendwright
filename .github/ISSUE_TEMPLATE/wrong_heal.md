---
name: A heal picked the wrong element
about: mendwright healed a selector, and the new one points at the wrong element
title: "Wrong heal: "
labels: bug
---

Do not paste page HTML, API keys or gateway keys here.
Page HTML can contain personal data that mendwright's redaction doesn't remove ([SECURITY.md](https://github.com/saadbuilt/mendwright/blob/main/SECURITY.md#redaction)).
These reports are the only accuracy data the project has, so even a short one helps.

## The step

- Key (the first argument to `heal.locator()`):
- Original selector:
- Healed XPath (from the `healed ...` line in stderr, or `mendwright report`):

## What it should have picked

Describe the element in words, for example "the Save button in the profile form, not the Save button in the footer".
Please don't paste its HTML.

## The heal in the journal

Paste the `heal.proposed` and `heal.applied` lines for this heal from `.mendwright/journal.jsonl`.
The `reasoning` field is the model's own text and can quote your page. Remove anything private first.

```
(journal lines)
```

## Model

- Provider and model id (for example `AnthropicProvider` and the model id you set):
- Sync (`mendwright.open`) or async (`mendwright.aio.open`):
- Playwright or Selenium, and the browser:

## mendwright doctor

Paste the output of `mendwright doctor`. It hides secret values and prints absolute paths; shorten them if you like.

```
(output of mendwright doctor)
```

## What happened next

- [ ] The step passed on the wrong element.
- [ ] The step failed, and the heal was quarantined.
- [ ] The wrong element was a destructive control (pay, delete and so on).
