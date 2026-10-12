"""mendwright: repair broken selectors in Playwright and Selenium scripts while they run.

When a selector stops matching, a model provider suggests a new XPath. mendwright checks
it against the captured HTML and the live page before saving it, then retries the step.
A key you mark as destructive (a pay, submit or delete control) is never healed.

The pipeline: resolve the candidate selectors (resolver) -> when none matches, capture a
redacted page bundle (capture) -> ask a provider for a fix (providers, prompt) -> check it
against the captured HTML and the live page (validation, gate) -> save it to the writable
override store (store) and retry the same step (coordinator) -> optionally publish
confirmed fixes to a seed file (publish).

Two extension points. Drivers (drivers/) talk to a browser library and are chosen by the
methods the page object has, not its module: sync Playwright (also camoufox and
patchright), async Playwright through mendwright.aio, and Selenium are built in, and
`register_driver` adds your own. Providers (providers/) talk to a model: mendwright.open()
picks one from the environment with auto_provider(), and GatewayProvider keeps the model
key on a server. The gateway URL, auth header, destructive keys, HTML and inventory
sources and the transaction guard are all passed in, never hardcoded.
"""

# mendwright logs to the 'mendwright' logger and adds no handler of its own. A heal that
# is applied or declined is a WARNING, so with no logging configured Python's last-resort
# handler prints that one line to stderr: a heal is never silent. Configure logging (for
# example logging.basicConfig(level=logging.INFO)) to format or route it, or raise the
# level (logging.getLogger('mendwright').setLevel(logging.ERROR)) to hide it.

from .capture import build_bundle, redact
from .client import DEFAULT_AUTH_HEADER, apply, report_outcome, request_heal
from .coordinator import HealCoordinator, resolve_with_heal
from .exceptions import AttentionRequired
from .publish import publish, republish_bundle
from .resolver import present, resolve
from .selectors import InvalidSelector
from .store import (
    SelectorStore,
    infer_role,
    load_store,
    merge,
    migrate_schema1,
)
from .validation import Result, validate
from .drivers import (
    Driver,
    PlaywrightSyncDriver,
    SeleniumDriver,
    register_driver,
    register_driver_front,
    wrap,
)
from .providers import (
    AnthropicProvider,
    BedrockProvider,
    GatewayProvider,
    GeminiProvider,
    HealProvider,
    LiteLLMProvider,
    LocalModelProvider,
    OpenAICompatibleProvider,
    auto_provider,
)
from .facade import HealEvent, Healer, open  # noqa: A004  (mendwright.open is the zero-config entry)
from . import aio  # noqa: E402,F401  (async Playwright: await mendwright.aio.open(page))

# 'open' is left out on purpose: `from mendwright import *` must not shadow builtins.open.
# Call it as mendwright.open(page).
__all__ = [
    'AttentionRequired', 'InvalidSelector',
    'Driver', 'PlaywrightSyncDriver', 'SeleniumDriver', 'wrap', 'register_driver', 'register_driver_front',
    'HealProvider', 'GatewayProvider', 'AnthropicProvider', 'OpenAICompatibleProvider',
    'GeminiProvider', 'BedrockProvider', 'LiteLLMProvider', 'LocalModelProvider', 'auto_provider',
    'Healer', 'HealEvent',
    'SelectorStore', 'load_store', 'merge', 'migrate_schema1', 'infer_role',
    'validate', 'Result',
    'build_bundle', 'redact',
    'request_heal', 'apply', 'report_outcome', 'DEFAULT_AUTH_HEADER',
    'HealCoordinator', 'resolve_with_heal',
    'resolve', 'present',
    'publish', 'republish_bundle',
]

# Single source of truth is pyproject's [project].version; read it from the installed
# distribution metadata so this constant never drifts. The literal fallback (kept in sync
# with pyproject, and pinned by tests/test_version.py) covers running straight from a source
# tree that was never installed.
try:
    from importlib.metadata import PackageNotFoundError, version as _dist_version
    try:
        __version__ = _dist_version('mendwright')
    except PackageNotFoundError:
        __version__ = '0.4.0'
except Exception:  # pragma: no cover - importlib.metadata is stdlib on 3.10+
    __version__ = '0.4.0'
