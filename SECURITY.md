# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue or pull request.
Use GitHub's private vulnerability reporting: open [this form](https://github.com/saadbuilt/mendwright/security/advisories/new), or click Report a vulnerability on the repository's security tab.

Include the mendwright version, the browser library and provider you used, what happened, and the smallest reproduction you have.
Remove page content, keys and tokens from anything you attach. [Redaction](#redaction) lists what mendwright itself doesn't remove.

I'll reply on the report itself, and we'll agree on the fix and a disclosure date there.

## Trust model

mendwright treats the model's output as untrusted.
The model sees a redacted copy of the page, and a hostile page can try to steer it.
A proposal is only a string. mendwright uses it after the local checks below pass, and the model never touches the page or the store.

## The checks

Every proposed XPath goes through `client.apply()`, which runs two checks.

`validate()` in `mendwright/validation.py` evaluates the XPath against the captured HTML with lxml. It rejects the proposal unless:

- it isn't a pure absolute-index path such as `/html[1]/body[1]/div[3]/input[1]`;
- it matches exactly one element;
- that element has the key's expected role (ARIA names such as `textbox` or `switch` are mapped to the element roles they mean);
- for a key marked destructive, the element's text is one of the expected texts. The coordinator refuses destructive keys before this point, so this rule only matters when you call `validate()` yourself.

`live_match()` in `mendwright/gate.py` then checks the XPath on the current page through the driver.
It must match exactly one element, that element must be visible unless its role is checkbox, and it must still have the key's role. For a destructive key, its text must also be one of the expected texts. Any driver error counts as no match.
`mendwright.open()` and `mendwright.aio.open()` always pass a driver. A `HealCoordinator` you build yourself runs this check only when you pass `driver=`.

`apply()` also rejects a proposal that the store holds as quarantined or superseded, or that reaches an element the key has quarantined, even through a different XPath. It never stores a selector that isn't XPath.
A proposal's frame hint is ignored; frame context is never taken from model output.

If the first proposal fails, the coordinator asks once more with the rejection reason. After that it raises `AttentionRequired`.

Other limits on a heal:

- `guard`: a callable passed to `mendwright.open(guard=...)`. While it returns `False`, nothing is healed and no model is called.
- Rate limit: direct providers get 3 heal attempts per key and 60 in total per hour by default (`rate_per_key_per_hour`, `rate_global_per_hour`). Every attempt that reaches the provider uses a slot, including rejected and dry-run ones, and makes up to 2 model calls. The limit is shared per store file within one process. When it's reached, the heal is declined.
- Confirmation: a new heal counts as confirmed only after `expect=` on the action passes, `.confirm(True)`, `Healer.confirm_all(True)`, or the pytest plugin sees the test pass. If the action fails in the browser, or `expect=` returns false, the healed selector is quarantined and isn't tried again.
- `MENDWRIGHT_DISABLE=1`: selectors still resolve, but nothing is healed and no provider is called.

## Destructive controls

A key that is destructive is never healed, and no model is called for it.
A key is destructive only when you mark it:

- the seed file marks it, or it is in `mendwright.open(destructive_keys=...)`. Nothing at run time can clear these.
- `heal.locator(..., destructive=True)` marks it. This is saved in the override file, and `destructive=False` clears it.

mendwright doesn't guess which controls are destructive from the key, `text=` or the page. A key you don't mark can be healed onto any element that has its role, including a button that pays or deletes.

A heal saved for a key before it was marked is quarantined, not acted on.
With the low-level `HealCoordinator`, the flag comes from `meta['destructive']`, which `store.meta(key)` fills from the seed file, `destructive_keys` and saved flags.

This is what a refused key looks like, from a run with a stub provider:

```
mendwright.exceptions.AttentionRequired: No matching element for: pay (tried 1 candidate(s)).
  -> 'pay' is marked as a destructive control, and mendwright never auto-heals those, because a validated but wrong selector here could act on a live account. Fix the selector in your code or seed file by hand.
```

The provider received 0 calls.

## Redaction

Redaction is **best-effort**. Visible text and most attribute values reach the model provider.

`build_bundle()` in `mendwright/capture.py` prepares the page in three steps:

1. `redact()`. It removes quoted `value="..."` and `value='...'` attributes. It replaces caller-supplied row secrets with `[redacted]`: the values in `row['secrets']` and `row['proxy']`, and every string under `row['profile']`, in plain and HTML-escaped form. It replaces absolute `http(s)://` URLs with `[URL]`, and runs of 12 to 19 digits with `[redacted number]`.
2. `clean_html()`. It removes `<script>`, `<style>`, `<noscript>`, `<template>`, comments and processing instructions, empties `<svg>`, removes `<meta>`, `<link>` and `<base>` from `<head>`, and collapses whitespace. If lxml can't parse the page, the text from step 1 is used as is.
3. `redact()` again. lxml writes every attribute with quotes, so an unquoted `value=` from the original page is removed here.

The inventory sent with the page never contains input values: the drivers don't read them, every string gets the same redaction, and keys named `value` are dropped.
The broken selectors and the expected text in the bundle get the same redaction as the page.
The key's saved fingerprint (the tag, stable attributes, text and label of the element the key last matched) goes into the bundle only for a provider that sets `wants_fingerprint = True`. Of the built-in providers only `LocalModelProvider` does, and it talks to Ollama on your machine. The fingerprint gets the same redaction.
The bundle for a list heal (`build_list_bundle()`) holds no page HTML and no inventory, only the page's repeated groups as above, with the same redaction, and the step's broken selectors, role and `intent=`.
The page URL isn't part of the prompt, so direct providers never send it. `GatewayProvider` posts the whole bundle, in which an http(s) page URL is replaced with `[URL]`. A URL with another scheme, such as `file://`, is sent as it is.
The local checks use the full, unredacted page; none of it is sent anywhere.

`redact()` on its own does step 1 only. It misses unquoted `value=` attributes and keeps scripts and `<meta>` tags.

This is real input and output for one element, with `row={'profile': {'name': {'first': 'Jane'}}}`:

```
in:  <span aria-label="Jane Doe" data-user="u42">jane@example.com 555-1234 4111111111111111 https://x.com/a?b=1</span>
out: <span aria-label="[redacted] Doe" data-user="u42">jane@example.com 555-1234 [redacted number] [URL]</span>
```

What isn't removed:

- visible text, including `<textarea>` and `contenteditable` content;
- `aria-label`, `title`, `placeholder`, `alt` and `data-*` values;
- email addresses, and numbers shorter than 12 digits;
- relative links with tokens in the query, such as `/reset?token=abc123`;
- anything in a page lxml can't parse, apart from step 1.

`mendwright.open()` doesn't take a `row`, so the facade has no row secrets to replace. The attribute, URL and number rules still apply. To redact specific values, pass `row=` to `resolve_with_heal()` on the low-level API.

Ways to send less:

- Use a model served by Ollama on your own machine (`OLLAMA_MODEL`, or mendwright's own selector model with `MENDWRIGHT_LOCAL_MODEL`), so the page doesn't leave it. The selector model is sent a short list of candidate elements instead of the page.
- On the low-level API, give `HealCoordinator` an `html_provider` that returns only the part of the page the key lives in.
- Set `MENDWRIGHT_DISABLE=1` where no page content may leave the machine. Dry run (`MENDWRIGHT_DRY_RUN=1`) still calls the provider.

## Provider error text

When a provider call fails, `provider.last_error` and the `AttentionRequired` message say why.
That text is built in `mendwright/providers/_errors.py`. It never includes the request body, and it masks:

- the provider's own API key;
- key-shaped tokens: `sk-`, `rk-`, `pk-`, `gsk-` and `xai-` prefixes, Google `AIza` keys, AWS `AKIA` and `ASIA` key ids, JWTs, and hex strings of 32 or more characters;
- `Bearer` and `Basic` values of 16 or more characters;
- echoed `api-key`, `x-goog-api-key`, `authorization`, `auth-token`, `access-token`, `secret` and `heal-key` values;
- the query parameters `key`, `api_key`, `token`, `access_token`, `sig`, `signature` and `x-amz-*`;
- URL user info and query strings, when the URL is shown as the request target.

Vendor messages are cut to 200 characters. For example:

```
in:  Incorrect API key provided: sk-proj-abc123def456ghi. Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123 see https://gw.example/heal?key=s3cr3tvalue&x=1
out: Incorrect API key provided: [redacted] Authorization: Bearer [redacted] see https://gw.example/heal?key=[redacted]&x=1
```

`mendwright doctor` adds its own masking. It replaces the value of any environment variable whose name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASS`, `AUTH` or `CREDENTIAL` with `***` wherever it appears in error text. It shows `MENDWRIGHT_GATEWAY` as scheme and host only, and shows `MENDWRIGHT_KEY` and other unlisted `MENDWRIGHT_*` variables as `(set, value hidden)`.

## Files mendwright writes

- `./.mendwright/overrides.json` (or `MENDWRIGHT_STORE`): selectors, their status, and each key's role, expected text, destructive flag, `intent=` text, fingerprint and list fingerprint. It holds no page HTML, but it can quote page text: a healed XPath often anchors on a label (for example `contains(normalize-space(.),'accept the terms')`), and a `text=` you pass is saved as `expectedText`.
- The fingerprint is saved for every key whose selector matched, not only for healed ones, while the fingerprint tier is on (the default). It holds the element's tag, its id, name, type, role, test ids, `aria-label`, `placeholder`, `title`, `alt` and `autocomplete`, the path of its link (no host, query or fragment), its non-generated class names, up to 80 characters of its visible text or of a form field's label (runs of 12 to 19 digits become `#`), and the tag, id, classes and role of up to 4 ancestors. It never holds an input's value. Visible text can still be personal, such as a name on an account page, so review the file before you commit it, or set `MENDWRIGHT_FINGERPRINT=0`. The fingerprint is never sent to a hosted model or a gateway. `LocalModelProvider`, which runs on your machine, gets it, redacted like the expected text.
- A list fingerprint is saved for every `heal.locator_all()` lookup that matched two or more elements while the fingerprint tier is on. `heal.page.locator(...).all()` and `heal.driver.find_elements()` save nothing. It holds the fingerprints of three of the items, as above, the tag names inside each of them and how many of each, how many items there were, the tag, id, test id, `aria-label`, non-generated class names and role of their container, the text of the list's label (the `aria-labelledby` or `aria-label` around it, or the nearest heading before it, up to 80 characters), and 4-byte hashes of the elements of the page outside the list (each one's tag, id, test id and class names, and its parent's) and of its headings and `aria-label`s, which say where the list was without holding that text. It is never sent to any provider: for a list heal, a model gets the page's repeated groups instead, each with its XPath, its container's opening tag and up to 100 characters of the text of its first two items, redacted like page HTML.
- `journal.jsonl` next to it: heal events with the key, selectors, provider class, model, latency, and the model's `reasoning` text, which can quote the page. With `dismiss=True`, a `flow.dismiss` event also holds up to 60 characters of the popup's text (runs of 12 to 19 digits redacted) and the labels of the buttons mendwright clicked on it, as the WARNING line does. On POSIX it's created with mode 0600.
- `overrides.json.lock` while a write is in progress.

A store file that is corrupt or can't be read raises an error. mendwright never treats it as empty and overwrites it.

## Residual risks

- The live check and the retried action are separate calls. The page can change between them.
- Only keys you mark are refused as destructive. An unmarked key can be healed onto a control that pays, submits or deletes, as long as it has the key's role. That holds for a fingerprint heal too, which needs no model and no key, though its rules ask for the same numbers and words in a text and refuse a match outside the recorded containers.
- A hostile page can steer the model toward a different element with the same role. The checks limit the role, the match count and visibility, but can't judge intent. Use `expect=` on actions that matter.
- A direct provider holds its API key in your process. Without the google-genai SDK, `GeminiProvider` falls back to a REST call that puts the key in the URL query (`?key=`), so a proxy that logs URLs can record it. Install `mendwright[gemini]` to have the SDK make the call instead. `GatewayProvider` keeps the model key on your server.
- `mendwright doctor` prints absolute paths for Python, the package and the store, which can contain your user name.
- With the pytest plugin, a passing test confirms its heals. For `GatewayProvider` that is an HTTP call to `/heal/outcome`. A test that passes for the wrong reason confirms a wrong heal.

If you find a way to get a selector past the checks, to heal a key marked destructive, or to send more page data than this page describes, please report it privately as described at the top.
