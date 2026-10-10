"""BedrockProvider: a direct, in-process provider over the AWS Bedrock Converse API.

Fully offline and deterministic: the boto3 client is injected and stubbed, so no real
network, AWS creds, or boto3 install is needed. The tests import the module directly.
"""

import sys

import pytest

from mendwright.prompt import build_prompt
from mendwright.providers.base import HealProvider
from mendwright.providers.bedrock import BedrockProvider

BUNDLE = {'key': 'terms', 'expectedRole': 'checkbox', 'expectedText': [],
          'candidates': ['//old1', '//old2'],
          'html': '<form><input type="checkbox" id="tc"></form>'}

REPLY = ('sure, here you go:\n'
         '{"xpath":"//input[@type=\'checkbox\']",'
         '"fallbacks":["//input[@id=\'tc\']"],"reasoning":"stable attrs"}')


class FakeBedrockClient:
    """Records the converse(**kwargs) call and returns a canned Converse response."""

    def __init__(self, text, blocks=None):
        self._content = blocks if blocks is not None else [{'text': text}]
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {'output': {'message': {'role': 'assistant', 'content': self._content}},
                'stopReason': 'end_turn',
                'usage': {'inputTokens': 11, 'outputTokens': 22}}


class RaisingClient:
    def converse(self, **kwargs):
        raise RuntimeError('ThrottlingException')


def test_propose_parses_canned_converse_response():
    prov = BedrockProvider(model='test-model', client=FakeBedrockClient(REPLY))
    assert prov.propose(BUNDLE) == {
        'xpath': "//input[@type='checkbox']",
        'fallbacks': ["//input[@id='tc']"],
        'reasoning': 'stable attrs',
    }


def test_propose_maps_build_prompt_onto_converse_request():
    client = FakeBedrockClient(REPLY)
    BedrockProvider(model='test-model', client=client).propose(BUNDLE)
    call = client.calls[0]
    expected = build_prompt(BUNDLE)
    assert call['modelId'] == 'test-model'
    assert call['system'] == [{'text': expected['system']}]
    assert call['messages'] == [{'role': 'user',
                                 'content': [{'text': expected['user']}]}]
    assert call['inferenceConfig']['maxTokens'] == 1024
    assert call['inferenceConfig']['temperature'] == 0


def test_propose_joins_multiple_content_blocks_skipping_reasoning():
    # A reasoning model may emit a reasoningContent block before the text answer.
    blocks = [{'reasoningContent': {'reasoningText': {'text': 'thinking...'}}},
              {'text': REPLY}]
    prov = BedrockProvider(model='test-model', client=FakeBedrockClient('', blocks=blocks))
    out = prov.propose(BUNDLE)
    assert out['xpath'] == "//input[@type='checkbox']"


def test_missing_model_raises_naming_the_env_var(monkeypatch):
    monkeypatch.delenv('BEDROCK_MODEL', raising=False)
    with pytest.raises(ValueError, match='BEDROCK_MODEL'):
        BedrockProvider(client=FakeBedrockClient(REPLY))


def test_model_read_from_env(monkeypatch):
    monkeypatch.setenv('BEDROCK_MODEL', 'env-model')
    client = FakeBedrockClient(REPLY)
    BedrockProvider(client=client).propose(BUNDLE)
    assert client.calls[0]['modelId'] == 'env-model'


def test_missing_boto3_raises_install_hint(monkeypatch):
    # Deterministic regardless of whether boto3 is actually installed: a None entry in
    # sys.modules makes `import boto3` raise ImportError. No client injected -> build path.
    monkeypatch.setitem(sys.modules, 'boto3', None)
    with pytest.raises(ImportError, match='boto3') as exc:
        BedrockProvider(model='test-model')
    # The hint names this distribution's extra (PyPI 'selfheal' is a different project).
    assert 'pip install "mendwright[bedrock]"' in str(exc.value)
    assert 'selfheal' not in str(exc.value)


def test_propose_returns_none_on_converse_error():
    assert BedrockProvider(model='test-model', client=RaisingClient()).propose(BUNDLE) is None


def test_propose_returns_none_on_unparseable_reply():
    prov = BedrockProvider(model='test-model', client=FakeBedrockClient('no json here'))
    assert prov.propose(BUNDLE) is None


def test_propose_returns_none_when_bundle_is_not_serializable():
    # A non-JSON-serializable inventory would raise inside build_prompt; propose must
    # swallow it and never reach converse (try_heal has no guard of its own).
    client = FakeBedrockClient(REPLY)
    prov = BedrockProvider(model='test-model', client=client)
    assert prov.propose(dict(BUNDLE, inventory=[object()])) is None
    assert client.calls == []


def test_report_outcome_is_a_safe_noop():
    prov = BedrockProvider(model='test-model', client=FakeBedrockClient(REPLY))
    assert prov.report_outcome('terms', '//x', True) is None


def test_satisfies_heal_provider_protocol():
    prov = BedrockProvider(model='test-model', client=FakeBedrockClient(REPLY))
    assert isinstance(prov, HealProvider)
