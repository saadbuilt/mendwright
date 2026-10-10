---
name: Bug report
about: Report a defect in mendwright
title: ""
labels: bug
---

Do not paste page HTML, capture bundles, API keys or gateway keys here.
Page HTML can contain personal data that mendwright's redaction doesn't remove ([SECURITY.md](https://github.com/saadbuilt/mendwright/blob/main/SECURITY.md#redaction)).
For a security problem, report it privately instead: https://github.com/saadbuilt/mendwright/security/advisories/new
If a heal picked the wrong element, use the "A heal picked the wrong element" template instead.

## What happened


## What you expected


## Steps to reproduce

A short script that runs offline is the quickest to fix.
Use a few lines of made-up HTML with `page.set_content()` rather than a real site.

1.
2.
3.

## mendwright doctor

Paste the output of `mendwright doctor`.
It lists the mendwright, Python, lxml and browser library versions, the provider and the store, and hides secret values.
It also prints absolute paths; shorten them if you like.

```
(output of mendwright doctor)
```

If you cannot run doctor, fill these in instead:

- mendwright version (`mendwright --version`):
- Python version:
- Browser library and version (Playwright, patchright, camoufox, Selenium, other):

And in every case:

- Sync (`mendwright.open`) or async (`mendwright.aio.open`):
- OS:

## Error and log lines

Paste the traceback and any `healed ...` or `did not heal ...` lines mendwright printed to stderr.
In a dry run, declined heals are logged at INFO, so add `logging.basicConfig(level=logging.INFO)` to see them.
Replace selectors, URLs and page text you don't want to make public.

```
(traceback and log lines)
```

## Checklist

- [ ] I removed page HTML, keys and tokens from this report.
- [ ] I ran `mendwright doctor` with the same Python and environment as the failing run.
