## mendwright heal report

Store: `<FOLDER>/overrides.json` with 3 key(s) and 5 selector(s).

| Key | Selector | Source | Status | Last success |
| --- | --- | --- | --- | --- |
| `accept_terms` (checkbox) | `#accept-terms` | code | active | 2026-10-01 08:00:00 UTC |
|  | `//label//input[@type='checkbox']` | heal | active | 2026-10-02 09:30:00 UTC |
|  | `//input[@name='terms']` | heal | **quarantined** | never |
| `go` (button, destructive, never auto-healed) | `//button[@id='go'] \| //a[@id='go']` | seed | active | never |
|  | `#old-go` in frame `//iframe[@id='shop']` | code | **superseded** | never |
| `pay_button` (button) | (none) | | | |

### Heals

| Requested | Applied | Rejected | Quarantined |
| ---: | ---: | ---: | ---: |
| 3 | 2 | 1 | 1 |

Last applied heals, newest first:

| When | Key | Old | New | Provider |
| --- | --- | --- | --- | --- |
| 2026-10-03 10:00:03 UTC | `accept_terms` | `#accept-terms (+2 more)` | `//label//input[@type='checkbox']` | AnthropicProvider, claude-test |
| 2026-10-02 09:30:00 UTC | `accept_terms` | `#accept-terms` | `//input[@name='terms']` | AnthropicProvider, claude-test |
