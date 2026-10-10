"""The demo behind `mendwright demo`: a broken selector healed, offline, in a few lines.

Everything it needs ships in the package (the two page versions are v1.html and v2.html
here), so it works right after `pip install mendwright`: no clone, no API key, no
browser. `mendwright demo --browser chromium` runs the same story in real Playwright.

Pieces you can reuse in your own experiments:

- StandInPage: a sync-Playwright-shaped page over lxml (XPath and '#id' CSS).
- ScriptedProvider: a provider that builds the real prompt and parses a canned reply.
- page_html('v1' | 'v2'): the demo pages.
- story(page, store_path, say=print): the story itself, on any sync Playwright page.
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
