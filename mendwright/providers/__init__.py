"""Heal providers: the model extension point.

GatewayProvider (the model key stays on your server) plus direct in-process providers for
Anthropic, OpenAI-compatible APIs (OpenAI, Azure, Ollama, Groq, OpenRouter, vLLM, LM
Studio), Gemini, Bedrock and LiteLLM. Importing a provider class never imports its vendor
SDK: SDKs load lazily inside methods, with a stdlib urllib fallback, so `import mendwright`
pulls in no vendor SDK. auto_provider() picks one from the environment; mendwright.open()
uses it when no provider= is passed. Each built-in provider exposes ``last_error`` (see
base.py).
"""

from .base import HealProvider
from .gateway import GatewayProvider
from .anthropic import AnthropicProvider
from .openai_compat import OpenAICompatibleProvider
from .gemini import GeminiProvider
from .bedrock import BedrockProvider
from .litellm import LiteLLMProvider
from .detect import auto_provider

__all__ = [
    'HealProvider', 'GatewayProvider', 'AnthropicProvider', 'OpenAICompatibleProvider',
    'GeminiProvider', 'BedrockProvider', 'LiteLLMProvider', 'auto_provider',
]
