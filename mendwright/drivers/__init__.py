"""Browser drivers: the only library-specific layer.

Built in: the sync Playwright API (also camoufox and patchright) and Selenium WebDriver.
Both are registered below and import their library lazily. wrap() handles sync objects
only: async Playwright pages go through mendwright.aio.open(), which uses
drivers/playwright_async.py. Add your own with register_driver(detect, make).
"""

from .base import (Driver, driver_act_on, driver_describe, driver_fingerprint,
                   driver_framed, driver_inventory, is_driver)
from .playwright_sync import PlaywrightSyncDriver, detect as _pw_detect
from .selenium import SeleniumDriver, detect as _sel_detect
from .registry import register_driver, register_driver_front, wrap

# Built-ins: Playwright sync family (also camoufox, patchright) + Selenium. Detection is
# disjoint (Playwright: locator+frame_locator on a Page/Frame; Selenium: find_elements +
# switch_to), so order is irrelevant. Both lazy-use their library, so importing mendwright
# needs neither installed.
register_driver(_pw_detect, PlaywrightSyncDriver)
register_driver(_sel_detect, SeleniumDriver)

__all__ = [
    'Driver', 'is_driver', 'wrap', 'register_driver', 'register_driver_front',
    'PlaywrightSyncDriver', 'SeleniumDriver',
    'driver_act_on', 'driver_describe', 'driver_fingerprint', 'driver_framed',
    'driver_inventory',
]
