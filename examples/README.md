# Examples

- `demo/`: the offline demo as a short script you can read and copy. See [demo/README.md](demo/README.md).
- `heal_gateway.mjs` and `wrangler.toml`: a Cloudflare Worker that keeps your model API key on a server instead of in each copy of your tool. Clients send it the same redacted page bundle a direct provider gets, and it rate-limits per key and in total. Setup is in the [guide](../docs/guide.md#direct-or-gateway). You don't need it to run mendwright on your own machine or in your CI, where a direct provider is simpler.

`tests/test_gateway_worker.py` runs the worker under node and checks that its prompt matches `mendwright/prompt.py`.
