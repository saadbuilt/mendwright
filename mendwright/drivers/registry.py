"""Driver auto-detection registry. wrap(obj) turns a raw page/session into a Driver by
walking the registered (detect, make) pairs; the first match wins, so callers pass a bare
page and name nothing. Detection looks at the methods the live object has, never at its
module name (camoufox and patchright are not 'playwright', and a custom wrapper has no
stable module)."""

from .base import is_driver, unsupported_message

_REGISTRY = []  # list[(detect, make)], checked in registration order


def register_driver(detect, make):
    """Register a driver: detect(obj)->bool, make(obj)->Driver. Later-registered custom
    drivers can be checked before built-ins by inserting at the front (see front=)."""
    _REGISTRY.append((detect, make))


def register_driver_front(detect, make):
    _REGISTRY.insert(0, (detect, make))


def wrap(obj):
    """Return obj if it is already a Driver, else the first registered driver that detects it.

    When nothing matches, the TypeError says why: an async page or driver, a Playwright
    Locator/FrameLocator passed where a page was expected, or an unknown object. Custom
    drivers registered with register_driver_front are still tried first, so one can take
    over any of those cases."""
    from ..proxies import unwrap
    obj = unwrap(obj)   # heal.page or heal.driver: drive the real page, never the proxy
    if is_driver(obj):
        return obj
    for detect, make in _REGISTRY:
        try:
            if detect(obj):
                return make(obj)
        except Exception:
            continue
    raise TypeError(unsupported_message(obj))
