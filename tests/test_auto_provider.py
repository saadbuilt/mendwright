"""auto_provider(): env-driven provider selection, fully offline and deterministic.

Most tests install a fake ``mendwright.providers.<module>`` into sys.modules (monkeypatched,
auto-restored) instead of using the real provider files, so they only check selection and
the arguments passed. The real providers used here (GatewayProvider and, for the Ollama
default host, OpenAICompatibleProvider) are pure to construct: no network until a heal is
requested. An autouse fixture scrubs every env var auto_provider reads so the host
machine's own ANTHROPIC_API_KEY (etc.) can't leak in and make a test flaky.
"""

import sys
import types

import pytest

from mendwright.providers.detect import auto_provider

# Every env var auto_provider consults. Cleared before each test.
_ALL_ENV = [
    'MENDWRIGHT_PROVIDER',
    'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL',
    'OPENAI_API_KEY', 'OPENAI_MODEL',
    'GEMINI_API_KEY', 'GEMINI_MODEL',
    'OLLAMA_HOST', 'OLLAMA_MODEL',
    'MENDWRIGHT_GATEWAY', 'MENDWRIGHT_KEY', 'MENDWRIGHT_AUTH_HEADER',
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ALL_ENV:
        monkeypatch.delenv(name, raising=False)


def _fake_provider_module(monkeypatch, module, class_name):
    """Install a fake mendwright.providers.<module> exposing a recording provider class, so
    the test is independent of the concurrently-written real sibling. Returns (class, captured)
    where captured records the args/kwargs the class (or its for_ollama factory) was built with."""
    captured = {}

    class Fake:
        def __init__(self, *args, **kwargs):
            captured['args'] = args
            captured['kwargs'] = kwargs

        @classmethod
        def for_ollama(cls, model, **kwargs):
            inst = cls(model=model, **kwargs)
            captured['for_ollama'] = True
            return inst

    Fake.__name__ = Fake.__qualname__ = class_name
    mod = types.ModuleType('mendwright.providers.' + module)
    setattr(mod, class_name, Fake)
    monkeypatch.setitem(sys.modules, 'mendwright.providers.' + module, mod)
    return Fake, captured


# -- each key selects the right provider class, with the model passed through -----------

def test_anthropic_key_selects_anthropic(monkeypatch):
    Fake, captured = _fake_provider_module(monkeypatch, 'anthropic', 'AnthropicProvider')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'claude-test')
    prov = auto_provider()
    assert isinstance(prov, Fake)
    assert captured['kwargs']['model'] == 'claude-test'
    assert 'api_key' not in captured['kwargs']  # provider reads its own key env var


def test_openai_key_selects_openai_compatible(monkeypatch):
    Fake, captured = _fake_provider_module(monkeypatch, 'openai_compat', 'OpenAICompatibleProvider')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-oai')
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-test')
    prov = auto_provider()
    assert isinstance(prov, Fake)
    assert captured['kwargs']['model'] == 'gpt-test'


def test_gemini_key_selects_gemini(monkeypatch):
    Fake, captured = _fake_provider_module(monkeypatch, 'gemini', 'GeminiProvider')
    monkeypatch.setenv('GEMINI_API_KEY', 'g-key')
    monkeypatch.setenv('GEMINI_MODEL', 'gemini-test')
    prov = auto_provider()
    assert isinstance(prov, Fake)
    assert captured['kwargs']['model'] == 'gemini-test'


def test_ollama_host_selects_keyless_openai_compatible(monkeypatch):
    Fake, captured = _fake_provider_module(monkeypatch, 'openai_compat', 'OpenAICompatibleProvider')
    monkeypatch.setenv('OLLAMA_HOST', 'http://box:11434')
    monkeypatch.setenv('OLLAMA_MODEL', 'llama-test')
    prov = auto_provider()
    assert isinstance(prov, Fake)
    assert captured.get('for_ollama') is True            # routed through the for_ollama factory
    assert captured['kwargs']['model'] == 'llama-test'
    assert captured['kwargs']['base_url'] == 'http://box:11434/v1'  # /v1 appended
    # api_key is '' (empty, not absent): the empty string stops OpenAICompatibleProvider
    # from falling back to OPENAI_API_KEY, so a local keyless host gets no auth header.
    assert captured['kwargs']['api_key'] == ''


def test_ollama_host_already_has_v1_suffix(monkeypatch):
    Fake, captured = _fake_provider_module(monkeypatch, 'openai_compat', 'OpenAICompatibleProvider')
    monkeypatch.setenv('OLLAMA_HOST', 'http://box:11434/v1/')
    monkeypatch.setenv('OLLAMA_MODEL', 'llama-test')
    auto_provider()
    assert captured['kwargs']['base_url'] == 'http://box:11434/v1'  # not doubled


def test_ollama_host_without_scheme_gets_http(monkeypatch):
    Fake, captured = _fake_provider_module(monkeypatch, 'openai_compat', 'OpenAICompatibleProvider')
    monkeypatch.setenv('OLLAMA_HOST', '127.0.0.1:11434')  # OLLAMA_HOST is conventionally scheme-less
    monkeypatch.setenv('OLLAMA_MODEL', 'llama-test')
    auto_provider()
    assert captured['kwargs']['base_url'] == 'http://127.0.0.1:11434/v1'


def test_forced_ollama_stays_keyless_despite_openai_key(monkeypatch):
    # Regression: forcing ollama while OPENAI_API_KEY is in the shell must NOT leak that key
    # to the local host (OpenAICompatibleProvider turns api_key=None into OPENAI_API_KEY).
    Fake, captured = _fake_provider_module(monkeypatch, 'openai_compat', 'OpenAICompatibleProvider')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-leak')
    monkeypatch.setenv('MENDWRIGHT_PROVIDER', 'ollama')
    monkeypatch.setenv('OLLAMA_MODEL', 'llama-test')
    auto_provider()
    assert captured['kwargs']['api_key'] == ''


# -- gateway branch: the REAL GatewayProvider, default vs custom auth header -------------

def test_gateway_env_selects_real_gateway_provider(monkeypatch):
    from mendwright.providers.gateway import GatewayProvider
    from mendwright.client import DEFAULT_AUTH_HEADER
    monkeypatch.setenv('MENDWRIGHT_GATEWAY', 'https://gw.example')
    monkeypatch.setenv('MENDWRIGHT_KEY', 'client-key')
    prov = auto_provider()
    assert isinstance(prov, GatewayProvider)
    assert prov.gateway == 'https://gw.example'
    assert prov.download_key == 'client-key'
    assert prov.auth_header == DEFAULT_AUTH_HEADER  # unset MENDWRIGHT_AUTH_HEADER keeps the default, not None


def test_gateway_custom_auth_header(monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_GATEWAY', 'https://gw.example')
    monkeypatch.setenv('MENDWRIGHT_AUTH_HEADER', 'X-Custom-Key')
    prov = auto_provider()
    assert prov.auth_header == 'X-Custom-Key'


def test_forced_gateway_without_url_raises_naming_var(monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_PROVIDER', 'gateway')  # forced, but no MENDWRIGHT_GATEWAY
    with pytest.raises(ValueError) as exc:
        auto_provider()
    assert 'MENDWRIGHT_GATEWAY' in str(exc.value)


# -- MENDWRIGHT_PROVIDER override + priority ----------------------------------------------

def test_mendwright_provider_overrides_detection(monkeypatch):
    anthropic_fake, _ = _fake_provider_module(monkeypatch, 'anthropic', 'AnthropicProvider')
    _, cap_openai = _fake_provider_module(monkeypatch, 'openai_compat', 'OpenAICompatibleProvider')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-oai')   # would auto-select openai...
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-test')
    monkeypatch.setenv('MENDWRIGHT_PROVIDER', 'anthropic')  # ...but this forces anthropic
    monkeypatch.setenv('ANTHROPIC_MODEL', 'claude-test')
    prov = auto_provider()
    assert isinstance(prov, anthropic_fake)
    assert 'kwargs' not in cap_openai  # openai provider never constructed


def test_priority_order_anthropic_beats_openai(monkeypatch):
    anthropic_fake, _ = _fake_provider_module(monkeypatch, 'anthropic', 'AnthropicProvider')
    _, cap_openai = _fake_provider_module(monkeypatch, 'openai_compat', 'OpenAICompatibleProvider')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'claude-test')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-oai')
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-test')
    prov = auto_provider()
    assert isinstance(prov, anthropic_fake)
    assert 'kwargs' not in cap_openai


def test_absent_provider_module_raises_naming_it(monkeypatch):
    # The guarded branch: a selected provider whose module is not in this build. A None entry
    # in sys.modules makes importlib raise ModuleNotFoundError(name='mendwright.providers.gemini'),
    # exercising the real guard deterministically whether or not the sibling has landed.
    monkeypatch.setitem(sys.modules, 'mendwright.providers.gemini', None)
    monkeypatch.setenv('GEMINI_API_KEY', 'g-key')
    monkeypatch.setenv('GEMINI_MODEL', 'gemini-test')
    with pytest.raises(ValueError) as exc:
        auto_provider()
    assert 'mendwright.providers.gemini' in str(exc.value)


def test_unknown_mendwright_provider_raises_listing_valid_names(monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_PROVIDER', 'bogus')
    with pytest.raises(ValueError) as exc:
        auto_provider()
    msg = str(exc.value)
    for name in ('anthropic', 'openai', 'gemini', 'gateway', 'ollama'):
        assert name in msg


# -- model handling ---------------------------------------------------------------------

def test_missing_model_raises_naming_the_var(monkeypatch):
    _fake_provider_module(monkeypatch, 'anthropic', 'AnthropicProvider')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant')  # key present, but no ANTHROPIC_MODEL
    with pytest.raises(ValueError) as exc:
        auto_provider()
    assert 'ANTHROPIC_MODEL' in str(exc.value)


def test_explicit_model_arg_overrides_env(monkeypatch):
    _fake, captured = _fake_provider_module(monkeypatch, 'anthropic', 'AnthropicProvider')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'env-model')
    auto_provider(model='arg-model')
    assert captured['kwargs']['model'] == 'arg-model'


def test_extra_kwargs_forwarded_to_provider(monkeypatch):
    _fake, captured = _fake_provider_module(monkeypatch, 'anthropic', 'AnthropicProvider')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'm')
    sentinel = object()
    auto_provider(http=sentinel, timeout=5)
    assert captured['kwargs']['http'] is sentinel
    assert captured['kwargs']['timeout'] == 5


# -- nothing configured -----------------------------------------------------------------

def test_empty_env_raises_listing_all_selector_vars():
    with pytest.raises(ValueError) as exc:
        auto_provider()
    msg = str(exc.value)
    for var in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY',
                'MENDWRIGHT_GATEWAY', 'OLLAMA_HOST', 'MENDWRIGHT_PROVIDER'):
        assert var in msg


def test_empty_env_error_names_every_env_var_auto_provider_reads():
    with pytest.raises(ValueError) as exc:
        auto_provider()
    msg = str(exc.value)
    for var in _ALL_ENV:  # keys, models, gateway settings, Ollama, and the override
        assert var in msg, var
    assert 'http://localhost:11434' in msg
    assert chr(0x2014) not in msg  # no em-dash


# -- Ollama on OLLAMA_MODEL alone ---------------------------------------------------------

def test_ollama_model_alone_selects_local_ollama(monkeypatch):
    # Most Ollama installs never set OLLAMA_HOST; OLLAMA_MODEL is enough. Uses the real
    # OpenAICompatibleProvider (pure to construct, no network).
    from mendwright.providers.openai_compat import OpenAICompatibleProvider
    monkeypatch.setenv('OLLAMA_MODEL', 'llama-test')
    prov = auto_provider()
    assert isinstance(prov, OpenAICompatibleProvider)
    assert prov.base_url == 'http://localhost:11434/v1'
    assert prov.model == 'llama-test'
    assert prov.api_key == ''


def test_ollama_model_alone_routes_through_for_ollama(monkeypatch):
    Fake, captured = _fake_provider_module(monkeypatch, 'openai_compat', 'OpenAICompatibleProvider')
    monkeypatch.setenv('OLLAMA_MODEL', 'llama-test')
    prov = auto_provider()
    assert isinstance(prov, Fake)
    assert captured.get('for_ollama') is True
    assert 'base_url' not in captured['kwargs']  # for_ollama's localhost default applies
    assert captured['kwargs']['api_key'] == ''


@pytest.mark.parametrize('module, class_name, env', [
    ('anthropic', 'AnthropicProvider', {'ANTHROPIC_API_KEY': 'sk-ant', 'ANTHROPIC_MODEL': 'm'}),
    ('openai_compat', 'OpenAICompatibleProvider', {'OPENAI_API_KEY': 'sk-oai', 'OPENAI_MODEL': 'm'}),
    ('gemini', 'GeminiProvider', {'GEMINI_API_KEY': 'g', 'GEMINI_MODEL': 'm'}),
])
def test_cloud_keys_still_win_over_ollama_model(monkeypatch, module, class_name, env):
    Fake, captured = _fake_provider_module(monkeypatch, module, class_name)
    monkeypatch.setenv('OLLAMA_MODEL', 'llama-test')
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    prov = auto_provider()
    assert isinstance(prov, Fake)
    assert captured.get('for_ollama') is None
    assert captured['kwargs']['model'] == 'm'


def test_gateway_still_wins_over_ollama_model(monkeypatch):
    from mendwright.providers.gateway import GatewayProvider
    monkeypatch.setenv('OLLAMA_MODEL', 'llama-test')
    monkeypatch.setenv('MENDWRIGHT_GATEWAY', 'https://gw.example')
    assert isinstance(auto_provider(), GatewayProvider)


def test_ollama_host_without_model_still_names_ollama_model(monkeypatch):
    _fake_provider_module(monkeypatch, 'openai_compat', 'OpenAICompatibleProvider')
    monkeypatch.setenv('OLLAMA_HOST', 'http://box:11434')
    with pytest.raises(ValueError) as exc:
        auto_provider()
    assert 'OLLAMA_MODEL' in str(exc.value)


def test_unknown_provider_lists_each_name_once(monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_PROVIDER', 'bogus')
    with pytest.raises(ValueError) as exc:
        auto_provider()
    assert str(exc.value).count('ollama') == 1
