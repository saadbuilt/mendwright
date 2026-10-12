"""GatewayProvider: for clients that should not hold a model key. It POSTs the redacted
capture bundle to a mendwright gateway (for example the Cloudflare Worker in examples/)
that holds the model API key on the server, so no provider key ships inside a distributed
client. A HealCoordinator given no provider= builds one from its gateway and download_key
arguments.

After each propose() call, ``last_error`` is None on success or a short reason when it
returned None, for example "the gateway returned HTTP 401 (unauthorized): auth." or
"the gateway did not return a selector (needs_human: rate limit)."
"""

from ..client import DEFAULT_AUTH_HEADER, report_outcome, request_heal
from ._errors import NOT_HERE_HINT, Describer, sentence

_HINTS = {
    'auth': 'Check MENDWRIGHT_KEY (download_key=) against the key the gateway expects.',
    'model': 'Check MENDWRIGHT_GATEWAY: it should be the gateway root URL, without /heal.',
    'connect': 'Check MENDWRIGHT_GATEWAY (the gateway URL) and that the gateway is deployed.',
    'rate': "Wait for the gateway's rate limit window to reset, then retry.",
    'server': "Check the gateway's logs (for the Cloudflare example: wrangler tail).",
    'body': 'Check that MENDWRIGHT_GATEWAY points at a mendwright gateway.',
    'human': 'Try a stronger model on the gateway, or repair the selector by hand.',
}


class _GatewayDescriber(Describer):
    def needs_human(self, reason):
        text = 'the gateway did not return a selector (needs_human'
        if isinstance(reason, str) and reason.strip():
            text += ': ' + self._clean(reason)
        text += ')'
        if isinstance(reason, str) and reason.startswith('the model says no element'):
            return sentence(text, NOT_HERE_HINT)   # the gateway passed the model's answer on
        rate = isinstance(reason, str) and 'rate' in reason.lower()
        return sentence(text, self.hints.get('rate' if rate else 'human'))

    def no_xpath(self):
        return sentence('the gateway reply did not include an xpath', self.hints.get('body'))


def gateway_errors(url, download_key=None):
    """The Describer request_heal() uses to word a gateway failure for ``url``."""
    return _GatewayDescriber('the gateway', url=url, hints=_HINTS, secrets=(download_key,))


class GatewayProvider:
    def __init__(self, gateway, download_key=None, *, http=None,
                 auth_header=DEFAULT_AUTH_HEADER, timeout=30):
        if not gateway:
            raise ValueError('GatewayProvider requires a gateway URL')
        self.gateway = gateway
        self.download_key = download_key
        self.http = http
        self.auth_header = auth_header
        self.timeout = timeout
        self.last_error = None

    def propose(self, bundle):
        self.last_error = None
        reasons = []
        proposal = request_heal(bundle, gateway=self.gateway, download_key=self.download_key,
                                http=self.http, timeout=self.timeout,
                                auth_header=self.auth_header, on_error=reasons.append)
        if proposal is None:
            self.last_error = (reasons[-1] if reasons
                               else 'the gateway did not return a usable selector.')
        return proposal

    def report_outcome(self, key, xpath, success):
        report_outcome(self.gateway, key, xpath, success, download_key=self.download_key,
                       http=self.http, auth_header=self.auth_header)
