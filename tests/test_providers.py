"""The model seam: GatewayProvider back-compat + the direct-provider (no-gateway) on-ramp."""

import pytest

from mendwright import GatewayProvider, HealCoordinator, SelectorStore


def _store(tmp_path):
    data = {'schema': 2, 'selectors': {'terms': {
        'expectedRole': 'checkbox', 'expectedText': [], 'destructive': False,
        'candidates': [{'xpath': "//old", 'source': 'seed', 'status': 'active'}]}}}
    return SelectorStore(data, str(tmp_path / 'o.json'))


HTML = '<form><input type="checkbox" id="tc"></form>'
HEALED = "//input[@type='checkbox']"


class DirectProvider:
    """A minimal in-process provider — the shape a direct Anthropic/OpenAI adapter takes."""
    def __init__(self, xpath):
        self.xpath = xpath
        self.outcomes = []
    def propose(self, bundle):
        return {'xpath': self.xpath, 'fallbacks': [], 'reasoning': 'direct'}
    def report_outcome(self, key, xpath, success):
        self.outcomes.append((key, xpath, success))


def test_coordinator_runs_with_a_direct_provider_no_gateway(tmp_path):
    store = _store(tmp_path)
    prov = DirectProvider(HEALED)
    coord = HealCoordinator(store, provider=prov,          # <-- no gateway/download_key at all
                            html_provider=lambda: HTML, inventory_provider=lambda: [],
                            guard=lambda: True)
    applied = coord.try_heal('terms', store.meta('terms'), [], {'url': 'x'})
    assert applied == HEALED                                # provider proposal validated + applied
    coord.pending['terms'] = applied
    coord.confirm_pending('terms', True)
    assert prov.outcomes == [('terms', HEALED, True)]       # outcome routed through the provider


def test_gateway_provider_requires_a_url():
    with pytest.raises(ValueError):
        GatewayProvider(None)


def test_coordinator_back_compat_builds_gateway_provider(tmp_path):
    # The historical (store, gateway, download_key, ...) signature still works.
    coord = HealCoordinator(_store(tmp_path), 'https://gw.example', 'key',
                            html_provider=lambda: HTML, inventory_provider=lambda: [],
                            guard=lambda: True)
    assert isinstance(coord.provider, GatewayProvider)
    assert coord.provider.gateway == 'https://gw.example'
