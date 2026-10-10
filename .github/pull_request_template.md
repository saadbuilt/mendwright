## Summary

What this changes and why.

Closes #

## Checklist

- [ ] `python -m pytest` passes, and new behaviour has offline tests (no network, real model, real browser, clock or randomness).
- [ ] `ruff check .` passes.
- [ ] No new required dependency. Any vendor SDK is imported inside the method that uses it, with a `urllib` fallback.
- [ ] No model id in library code.
- [ ] The checks are not weakened: a provider only proposes, and `validate()`, `live_match()`, the destructive-control refusal and `guard` still apply.
- [ ] If `mendwright/prompt.py` changed, `examples/heal_gateway.mjs` changed to match, and `tests/test_gateway_worker.py` passes with node installed.
- [ ] If a driver changed, `python -m pytest -m browser` passes for the engines I have.
- [ ] If the change is user-visible, `CHANGELOG.md` has an entry under the next version.

## Testing

The commands you ran and their results.
