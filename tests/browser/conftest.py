"""Fixtures for the real-browser suite. Run it with: python -m pytest -m browser

- engine: one browser per engine for the whole session, parametrized over every engine in
  mw_engines.ENGINE_IDS. pytest groups the tests by engine, so only one browser is open at
  a time. An engine that is not installed here skips its tests with the reason.
- tab: a fresh page on that engine for each test.

A test marked @pytest.mark.engine_family('playwright') (or 'selenium') runs only on that
family; the other engines are deselected rather than skipped, since the test does not
apply to them.

Nothing here imports playwright or selenium at module level. The default run (which
deselects these tests) still collects this directory in jobs that have neither installed.
"""

import os
import sys

import pytest

# The default import mode puts this folder on sys.path already; --import-mode=importlib
# does not, and the helper module must still import.
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mw_engines import ENGINE_IDS, family_of, make_engine  # noqa: E402

# A real model is never called from these tests: every open() passes provider=. Clearing
# the provider settings also keeps auto_provider() from finding one by accident.
_PROVIDER_ENV_PREFIXES = ('ANTHROPIC_', 'OPENAI_', 'GEMINI_', 'BEDROCK_', 'LITELLM_',
                          'OLLAMA_', 'MENDWRIGHT_GATEWAY', 'MENDWRIGHT_KEY',
                          'MENDWRIGHT_AUTH_HEADER', 'MENDWRIGHT_PROVIDER')


def pytest_configure(config):
    config.addinivalue_line(
        'markers', "engine_family(*families): run a browser test only on these engine "
                   "families ('playwright', 'selenium')")


def pytest_collection_modifyitems(config, items):
    """Deselect engine/test pairs that do not apply, such as a Playwright-only test on
    Selenium. Every test keeps the full engine list, so the session-scoped engine fixture
    stays grouped and each browser starts once."""
    keep, drop = [], []
    for item in items:
        marker = item.get_closest_marker('engine_family')
        callspec = getattr(item, 'callspec', None)
        engine_id = callspec.params.get('engine') if callspec is not None else None
        applies = (marker is None or engine_id not in ENGINE_IDS
                   or family_of(engine_id) in marker.args)
        if not applies:
            drop.append(item)
        else:
            keep.append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


@pytest.fixture(autouse=True)
def _no_model_provider_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(_PROVIDER_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)


# Engines that must start. CI sets this so a browser that fails to launch on the runner
# fails the job instead of quietly skipping every test for that engine.
_REQUIRED = {e.strip() for e in os.environ.get('MENDWRIGHT_REQUIRE_ENGINES', '').split(',') if e.strip()}


@pytest.fixture(scope='session', params=ENGINE_IDS)
def engine(request):
    eng = make_engine(request.param)
    try:
        eng.start()
    except pytest.skip.Exception as exc:
        if request.param in _REQUIRED:
            pytest.fail(request.param + ' is required (MENDWRIGHT_REQUIRE_ENGINES) but did not '
                        'start: ' + str(exc), pytrace=False)
        raise
    yield eng
    eng.stop()


@pytest.fixture
def tab(engine, tmp_path):
    workdir = tmp_path / 'pages'
    workdir.mkdir()
    fresh = engine.new_tab(workdir)
    yield fresh
    fresh.close()


@pytest.fixture
def store_file(tmp_path):
    """Where this test's override store lives. The journal is written next to it."""
    return str(tmp_path / 'store' / 'overrides.json')
