"""The browser `mendwright check` loads pages in: sync Playwright, one browser per worker
thread (sync Playwright is per thread), and a new context for every page, so nothing one
page sets reaches another.

Each context gets the storage state file passed with --storage-state, which Playwright
reads and never writes, refuses downloads, and keeps Playwright's default viewport unless
--viewport is given. No screenshot, trace or HTML is saved. Playwright is imported inside
start(), as in mendwright.demo, so importing this module needs no browser library.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

NO_PLAYWRIGHT = ('mendwright check needs Playwright to load pages, and it is not installed. '
                 'Install it with: pip install "mendwright[playwright]", then: python -m '
                 'playwright install chromium')


class SetupError(Exception):
    """The browser could not be started: `mendwright check` exits 4 with this text."""


@dataclass
class Visit:
    """One page loaded in its own context: the Playwright page, the HTTP status of the
    navigation's response (None when there was none), what the navigation raised, if
    anything, and how long it took. close() closes the context."""
    page: Any
    status: int | None
    error: BaseException | None
    seconds: float
    context: Any = None

    def close(self):
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass


def _first(exc):
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0]


class PlaywrightSession:
    """One worker's Playwright and browser. start() raises SetupError; visit() loads one
    address in a new context; close() closes the browser and stops Playwright, and is safe
    to call after a start() that failed."""

    def __init__(self, browser='chromium', *, headless=True, viewport=None, storage_state=None,
                 nav_timeout_ms=30000, scrub=None):
        self.browser = browser
        self.headless = headless
        self.viewport = viewport
        self.storage_state = storage_state
        self.nav_timeout_ms = nav_timeout_ms
        self._scrub = scrub or (lambda text: text)
        self._pw = None
        self._browser = None

    def start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SetupError(NO_PLAYWRIGHT) from None
        try:
            self._pw = sync_playwright().start()
        except Exception as exc:
            raise SetupError('could not start Playwright (' + self._scrub(_first(exc)) + '). Run '
                             'it outside an asyncio event loop, or reinstall it with: pip '
                             'install -U playwright') from None
        try:
            self._browser = getattr(self._pw, self.browser).launch(headless=self.headless)
        except Exception as exc:
            text = str(exc)
            if "Executable doesn't exist" in text or 'playwright install' in text:
                raise SetupError("Playwright's " + self.browser + ' browser is not installed. '
                                 'Install it with: python -m playwright install '
                                 + self.browser) from None
            raise SetupError('could not start ' + self.browser + ' (' + self._scrub(_first(exc))
                             + '). Check that it runs, or reinstall it with: python -m '
                             'playwright install ' + self.browser) from None

    def visit(self, url, wait_until='load'):
        """Load url in a new context and return a Visit, also when the navigation failed
        (Visit.error). The caller closes it."""
        options = {'accept_downloads': False}
        if self.storage_state:
            options['storage_state'] = self.storage_state
        if self.viewport:
            options['viewport'] = {'width': self.viewport[0], 'height': self.viewport[1]}
        context = self._browser.new_context(**options)
        try:
            context.set_default_navigation_timeout(self.nav_timeout_ms)
            page = context.new_page()
            started = time.monotonic()
            response, error = None, None
            try:
                response = page.goto(url, wait_until=wait_until, timeout=self.nav_timeout_ms)
            except Exception as exc:
                error = exc
            status = getattr(response, 'status', None) if response is not None else None
            return Visit(page, status if isinstance(status, int) else None, error,
                         time.monotonic() - started, context)
        except BaseException:
            try:
                context.close()
            except Exception:
                pass
            raise

    def close(self):
        for close in (getattr(self._browser, 'close', None), getattr(self._pw, 'stop', None)):
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        self._browser = self._pw = None
