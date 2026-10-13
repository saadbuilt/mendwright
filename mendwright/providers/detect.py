"""auto_provider(): choose a HealProvider from the environment.

A zero-config on-ramp for the direct (no-gateway) providers: it reads whichever
credential is present and constructs the matching provider, importing that
provider's module lazily (by name, via importlib) so ``import mendwright`` never
pulls in a vendor SDK and an unconfigured provider costs nothing to have around.

Selection, in priority order when several are set:

    ANTHROPIC_API_KEY              -> AnthropicProvider        (model: ANTHROPIC_MODEL)
    OPENAI_API_KEY                 -> OpenAICompatibleProvider (model: OPENAI_MODEL)
    GEMINI_API_KEY                 -> GeminiProvider           (model: GEMINI_MODEL)
    MENDWRIGHT_GATEWAY             -> GatewayProvider          (key: MENDWRIGHT_KEY; the model
                                                                lives server-side)
    MENDWRIGHT_LOCAL_MODEL         -> LocalModelProvider       (mendwright's own selector model
                                                                in Ollama; host: OLLAMA_HOST)
    OLLAMA_MODEL or OLLAMA_HOST    -> OpenAICompatibleProvider.for_ollama
                                      (keyless; model: OLLAMA_MODEL; host: OLLAMA_HOST,
                                       default http://127.0.0.1:11434)

Cloud keys and the gateway win over Ollama, so a shell that has OLLAMA_MODEL set for
other tools still uses ANTHROPIC_API_KEY when that is set too.

``MENDWRIGHT_PROVIDER`` forces one branch (anthropic|openai|gemini|gateway|local|ollama),
overriding auto-detection. No vendor model ID is ever hardcoded: a direct provider's
model comes from the ``model`` argument or its ``*_MODEL`` env var, and a clear error
names the var when neither is set. The one default is the name of mendwright's own local
model: MENDWRIGHT_PROVIDER=local uses MENDWRIGHT_LOCAL_MODEL, or 'mendwright-selector'
when that is unset. With nothing configured, the error lists every env var
auto_provider reads. Like every provider, the result only proposes; the coordinator
re-validates before anything is applied.
"""

import importlib
import os

# provider name -> the env vars whose presence auto-selects it, in priority order.
_SELECT_ENV = (
    ('anthropic', ('ANTHROPIC_API_KEY',)),
    ('openai', ('OPENAI_API_KEY',)),
    ('gemini', ('GEMINI_API_KEY',)),
    ('gateway', ('MENDWRIGHT_GATEWAY',)),
    ('local', ('MENDWRIGHT_LOCAL_MODEL',)),
    ('ollama', ('OLLAMA_MODEL', 'OLLAMA_HOST')),
)
_NAMES = tuple(name for name, _ in _SELECT_ENV)

_NOTHING_CONFIGURED = (
    'mendwright.auto_provider: no provider configured. Set one of these, checked in this '
    'order: ANTHROPIC_API_KEY and ANTHROPIC_MODEL (Anthropic); OPENAI_API_KEY and '
    'OPENAI_MODEL (OpenAI-compatible); GEMINI_API_KEY and GEMINI_MODEL (Gemini); '
    'MENDWRIGHT_GATEWAY and MENDWRIGHT_KEY, plus MENDWRIGHT_AUTH_HEADER if your gateway '
    'uses another header (mendwright gateway); MENDWRIGHT_LOCAL_MODEL (mendwright\'s local '
    'selector model in Ollama); OLLAMA_MODEL, plus OLLAMA_HOST if Ollama '
    'is not on http://127.0.0.1:11434 (local Ollama). Or set MENDWRIGHT_PROVIDER to one '
    'of: ' + ', '.join(_NAMES) + ', or pass provider=... to mendwright.open().')


def _env(name):
    """os.environ.get, read at call time, with the empty string treated as unset."""
    value = os.environ.get(name)
    return value if value else None


def _load(module, *class_names):
    """Lazily import ``mendwright.providers.<module>`` and return the first class present.

    A sibling that is not part of this build raises a clear error naming the module;
    a genuinely missing dependency OF the sibling (a different module) is re-raised
    untouched, so a broken provider is never silently skipped."""
    full = 'mendwright.providers.' + module
    try:
        mod = importlib.import_module(full)
    except ModuleNotFoundError as exc:
        if exc.name == full:
            raise ValueError(
                'mendwright.auto_provider: provider module ' + full +
                ' is not available in this build.') from exc
        raise
    for name in class_names:
        cls = getattr(mod, name, None)
        if cls is not None:
            return cls
    raise ValueError('mendwright.auto_provider: ' + full + ' exposes none of: ' +
                     ', '.join(class_names) + '.')


def _require_model(env_var, model):
    """The model from the explicit arg or ``env_var``, never baked in. Raises a clear
    error naming the env var when neither is set (satisfies the no-hardcoded-model rule
    locally, so the error is identical regardless of the provider's own behaviour)."""
    resolved = model or _env(env_var)
    if not resolved:
        raise ValueError(
            'mendwright.auto_provider: no model configured for this provider. Pass '
            'model=... to auto_provider() or set the ' + env_var + ' environment variable '
            "(use a current id from your provider's model list; mendwright has no default model).")
    return resolved


def _anthropic(model, kwargs):
    cls = _load('anthropic', 'AnthropicProvider')
    return cls(model=_require_model('ANTHROPIC_MODEL', model), **kwargs)


def _openai(model, kwargs):
    cls = _load('openai_compat', 'OpenAICompatibleProvider')
    return cls(model=_require_model('OPENAI_MODEL', model), **kwargs)


def _gemini(model, kwargs):
    # Gemini ships as its own leaf; accept the house-style class name and its shorthand.
    cls = _load('gemini', 'GeminiProvider', 'Gemini')
    return cls(model=_require_model('GEMINI_MODEL', model), **kwargs)


def _gateway(model, kwargs):
    # Gateway holds the model (and key) server-side, so no *_MODEL is read here.
    gateway = _env('MENDWRIGHT_GATEWAY')
    if not gateway:
        raise ValueError(
            'mendwright.auto_provider: the gateway provider requires the MENDWRIGHT_GATEWAY '
            'environment variable (the heal gateway URL).')
    auth_header = _env('MENDWRIGHT_AUTH_HEADER')
    if auth_header:
        kwargs.setdefault('auth_header', auth_header)  # else keep GatewayProvider's default
    cls = _load('gateway', 'GatewayProvider')
    return cls(gateway, _env('MENDWRIGHT_KEY'), **kwargs)


def _ollama(model, kwargs):
    # Local, keyless Ollama via the OpenAI-compatible adapter's for_ollama() factory.
    cls = _load('openai_compat', 'OpenAICompatibleProvider')
    model = _require_model('OLLAMA_MODEL', model)
    # Keyless by default: pass '' (not None) so OpenAICompatibleProvider does not fall back
    # to os.environ['OPENAI_API_KEY'] and leak a cloud key to the local host. A caller who
    # really wants auth can still pass api_key=... through kwargs.
    kwargs.setdefault('api_key', '')
    host = _env('OLLAMA_HOST')
    if host:  # unset: for_ollama's own default, http://127.0.0.1:11434/v1
        # Same reading of OLLAMA_HOST as the local provider (scheme-less values, a /v1
        # suffix, localhost and 0.0.0.0 to 127.0.0.1), plus the OpenAI-compatible /v1 path.
        from .local import _host
        kwargs.setdefault('base_url', _host(host) + '/v1')
    return cls.for_ollama(model, **kwargs)


def _local(model, kwargs):
    # mendwright's own selector model in Ollama. Keyless and local; the model name has a
    # default, so MENDWRIGHT_PROVIDER=local works with nothing else set.
    cls = _load('local', 'LocalModelProvider')
    host = _env('OLLAMA_HOST')
    if host:
        kwargs.setdefault('base_url', host)
    return cls(model=model or _env('MENDWRIGHT_LOCAL_MODEL'), **kwargs)


_BUILDERS = {
    'anthropic': _anthropic,
    'openai': _openai,
    'gemini': _gemini,
    'gateway': _gateway,
    'local': _local,
    'ollama': _ollama,
}


def auto_provider(model=None, **kwargs):
    """Construct the HealProvider selected by the environment (see the module docstring).

    ``model`` overrides the provider's ``*_MODEL`` env var; extra keyword arguments
    (e.g. ``http=`` for an injected transport, ``timeout=``) are forwarded to the chosen
    provider's constructor. Raises ValueError with a helpful message when
    ``MENDWRIGHT_PROVIDER`` names an unknown provider, or when nothing is configured."""
    forced = _env('MENDWRIGHT_PROVIDER')
    if forced:
        name = forced.strip().lower()
        if name not in _BUILDERS:
            raise ValueError(
                'mendwright.auto_provider: MENDWRIGHT_PROVIDER=' + forced + ' is not recognised. '
                'Valid values: ' + ', '.join(_NAMES) + '.')
        return _BUILDERS[name](model, dict(kwargs))
    for name, env_vars in _SELECT_ENV:
        if any(_env(var) for var in env_vars):
            return _BUILDERS[name](model, dict(kwargs))
    raise ValueError(_NOTHING_CONFIGURED)
