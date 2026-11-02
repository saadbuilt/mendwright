"""The demo behind `mendwright demo`: a redesign breaks two selectors, and your model
repairs one while mendwright refuses the other.

Everything it needs ships in the package (the two page versions are v1.html and v2.html
here), so it works right after `pip install mendwright`: no clone and no browser. It asks
the model provider from your environment, the one `mendwright doctor` shows; `mendwright
demo --scripted` uses a canned reply instead, with no model call and no key.
`mendwright demo --browser chromium` runs the same story in real Playwright. It prints
mendwright's own log lines from the run.

Pieces you can reuse in your own experiments:

- StandInPage: a sync-Playwright-shaped page over lxml (XPath and '#id' CSS).
- ScriptedProvider: a provider that builds the real prompt and parses a canned reply.
- page_html('v1' | 'v2'): the demo pages.
- story(page, store_path, provider=None): the story itself, on any sync Playwright page.
- run(...): what the command runs, returning its exit code.
"""

from ._page import StandInLocator, StandInPage
from ._story import (
    HEALED_XPATH,
    PAY_KEY,
    PAY_SELECTOR,
    SCRIPTED_REPLY,
    TERMS_KEY,
    TERMS_SELECTOR,
    ScriptedProvider,
    StoryResult,
    page_html,
    run,
    story,
)

__all__ = [
    'StandInPage', 'StandInLocator', 'ScriptedProvider', 'StoryResult', 'page_html', 'story',
    'run', 'HEALED_XPATH', 'SCRIPTED_REPLY', 'TERMS_KEY', 'TERMS_SELECTOR', 'PAY_KEY',
    'PAY_SELECTOR',
]
