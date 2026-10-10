"""The model extension point: how mendwright gets a repaired-selector proposal.

A HealProvider turns a capture bundle into a proposal dict {xpath, fallbacks?, reasoning?}
(or None when it can't), and records the canary outcome. Everything the provider returns
is untrusted: the coordinator checks every proposal locally before it is used. Built-ins:
GatewayProvider (the model key stays on your server) and direct in-process providers
(Anthropic, OpenAI-compatible, Gemini, Bedrock, LiteLLM). A HealCoordinator given no
provider= builds a GatewayProvider from its gateway and download_key arguments;
mendwright.open() uses auto_provider() instead.

Every built-in provider also has a ``last_error`` attribute (str or None). propose() sets it
to None when it returns a proposal and to a short, readable reason when it returns None,
such as "Anthropic returned HTTP 401 (authentication_error): invalid x-api-key. ...".
The reason never contains an API key or a request body. ``last_error`` is optional for
custom providers, so it is not part of the Protocol below; read it with
``getattr(provider, 'last_error', None)``.
"""

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class HealProvider(Protocol):
    def propose(self, bundle: dict) -> Optional[dict]: ...
    def report_outcome(self, key: str, xpath: str, success: bool) -> None: ...
