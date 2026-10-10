"""BedrockProvider: a direct, in-process HealProvider backed by AWS Bedrock's Converse
API (via boto3). boto3 is imported lazily. Bedrock requests are SigV4-signed, so there is
no stdlib fallback, but `import mendwright` still never requires boto3; only constructing a
BedrockProvider without boto3 installed raises an install hint.

The model id is never hardcoded: pass model=... or set BEDROCK_MODEL (construction raises
an error naming that env var if neither is given). Region comes from region=... or
AWS_REGION; when absent, boto3's own region resolution still applies. The request is built
from the shared build_prompt() and the reply parsed with parse_proposal(), so prompt
behaviour matches every other provider and the gateway. Like all providers it only
proposes {xpath, fallbacks, reasoning} or None; the coordinator re-validates before
anything is applied.

After each propose() call, ``last_error`` is None on success or a short reason when it
returned None, for example "Bedrock returned HTTP 403 (AccessDeniedException): ...".
botocore errors are recognised by shape, so this module never imports botocore.
"""

import os

from ..prompt import build_prompt, parse_proposal
from ._errors import Describer, sentence

_HINTS = {
    'auth': 'Check your AWS credentials (for example AWS_PROFILE) and that the model is '
            'enabled for your account in the Bedrock console.',
    'model': 'Check the model id in BEDROCK_MODEL (or model=) and AWS_REGION.',
    'connect': 'Check AWS_REGION (or region=) and your network connection.',
    'reply': 'Try a stronger model (BEDROCK_MODEL or model=).',
}

_NO_CREDENTIALS = ('NoCredentialsError', 'PartialCredentialsError', 'CredentialRetrievalError')


class BedrockProvider:
    def __init__(self, model=None, *, region=None, client=None,
                 max_tokens=1024, temperature=0):
        # Resolve the model before anything touches boto3, so a missing model raises the
        # right error even on a machine without boto3 installed.
        self.model = model or os.environ.get('BEDROCK_MODEL')
        if not self.model:
            raise ValueError(
                'BedrockProvider requires a model id: pass model=... or set the '
                'BEDROCK_MODEL environment variable (a Bedrock model id or, for models '
                'that require one, an inference-profile id).')
        self.region = region or os.environ.get('AWS_REGION')
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.last_error = None
        # Injectable for tests. Only build a real client (which needs boto3) when the
        # caller did not provide one; an injected client must never trigger the import.
        self.client = client if client is not None else self._build_client()

    def _build_client(self):
        try:
            import boto3  # lazy: Bedrock needs SigV4 signing, no stdlib fallback
        except ImportError as exc:
            raise ImportError(
                'BedrockProvider needs boto3, because Bedrock requests are SigV4-signed and '
                'there is no stdlib fallback. Install it with: pip install "mendwright[bedrock]"'
            ) from exc
        kwargs = {}
        if self.region:
            kwargs['region_name'] = self.region
        return boto3.client('bedrock-runtime', **kwargs)

    def _errors(self):
        who = 'Bedrock in ' + self.region if self.region else 'Bedrock'
        return Describer(who, hints=_HINTS)

    def propose(self, bundle):
        # Never raises: HealCoordinator.try_heal calls propose() without its own guard.
        # Every failure returns None and leaves a readable reason in self.last_error.
        self.last_error = None
        try:
            prompt = build_prompt(bundle)  # a non-serializable inventory raises here
        except Exception as exc:
            self.last_error = self._errors().prompt(exc)
            return None
        try:
            response = self.client.converse(
                modelId=self.model,
                system=[{'text': prompt['system']}],
                messages=[{'role': 'user', 'content': [{'text': prompt['user']}]}],
                inferenceConfig={'maxTokens': self.max_tokens,
                                 'temperature': self.temperature},
            )
        except Exception as exc:
            self.last_error = self._describe(exc)
            return None
        text = _output_text(response)
        if not text:
            stop = response.get('stopReason') if isinstance(response, dict) else None
            self.last_error = self._errors().no_text(
                ('stop reason: ' + stop) if isinstance(stop, str) and stop else None)
            return None
        proposal = parse_proposal(text)
        if proposal is None:
            self.last_error = self._errors().reply(text)
        return proposal

    def _describe(self, exc):
        if type(exc).__name__ in _NO_CREDENTIALS:
            return sentence('no AWS credentials were found for Bedrock',
                            'Configure them, for example with AWS_PROFILE or '
                            'AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.')
        return self._errors().exception(exc)

    def report_outcome(self, key, xpath, success):
        # Bedrock has no outcome endpoint; the canary is recorded by the store/coordinator.
        # The method exists to satisfy the HealProvider protocol.
        pass


def _output_text(response):
    """Join every text block of a Converse reply. Skips non-text blocks (e.g. a
    reasoningContent block a reasoning model may emit first), so parse_proposal sees the
    model's actual JSON answer rather than a KeyError or the wrong block."""
    try:
        content = response['output']['message']['content']
    except (KeyError, TypeError):
        return ''
    if not isinstance(content, list):
        return ''
    return ''.join(block['text'] for block in content
                   if isinstance(block, dict) and isinstance(block.get('text'), str))
