"""Canonical heal prompt: build the model request from a capture bundle, and parse the
model's reply into a proposal dict {xpath, fallbacks, reasoning}.

This is the only prompt in mendwright. Every direct provider builds its request from
build_prompt() and parses the reply with parse_proposal(), so the prompt is the same for
Anthropic, OpenAI-compatible APIs, Gemini, Bedrock and LiteLLM. The reference gateway
(examples/heal_gateway.mjs) has a JavaScript copy of both functions. A change here needs
the same change there; when node is installed, tests/test_gateway_worker.py runs the
worker and fails if the two differ.
"""

import json
import re

_MAX = 60000


def build_prompt(bundle):
    """Return {'system', 'user'} for a chat/messages request. Pure function of the bundle."""
    destructive = bool(bundle.get('destructive'))
    expected_role = bundle.get('expectedRole')
    expected_text = bundle.get('expectedText') or []
    system = (
        'You repair ONE broken web-automation xpath selector. Reply with ONLY a JSON object: '
        '{"xpath":"...","fallbacks":["..."],"reasoning":"..."}. '
        'Prefer role + nearby visible label text + stable attributes (href, formcontrolname, type) '
        'over structure. NEVER return absolute positional paths like /div[3]/div[2]. '
        "The xpath must match EXACTLY ONE element whose role is '" + str(expected_role) + "'."
    )
    if destructive:
        system += (' This is a DESTRUCTIVE control: its exact visible text MUST be one of '
                   + json.dumps(expected_text) + '.')
    user = (
        'key: ' + str(bundle.get('key')) +
        '\nexpectedRole: ' + str(expected_role) +
        '\nexpectedText: ' + json.dumps(expected_text) +
        '\ndestructive: ' + ('true' if destructive else 'false') +
        '\nbroken candidates:\n' + '\n'.join(bundle.get('candidates') or []) +
        (('\nYour previous proposal ' + str(bundle.get('rejectedXpath')) + ' was rejected: '
          + str(bundle.get('rejectReason')) + '. Propose a DIFFERENT selector.')
         if bundle.get('rejectReason') else '') +
        '\nDOM inventory (redacted):\n' + json.dumps(bundle.get('inventory') or [])[:_MAX] +
        '\nHTML snapshot (redacted):\n' + str(bundle.get('html') or '')[:_MAX]
    )
    return {'system': system, 'user': user}


def parse_proposal(text):
    """Extract {xpath, fallbacks, reasoning} from a model reply, or None. Tolerates text
    around the JSON object. Returns None unless a non-empty xpath is present."""
    if not text:
        return None
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except Exception:
        return None
    if not isinstance(obj, dict) or not obj.get('xpath'):
        return None
    fallbacks = obj.get('fallbacks')
    return {
        'xpath': str(obj['xpath']),
        'fallbacks': [str(f) for f in fallbacks[:4]] if isinstance(fallbacks, list) else [],
        'reasoning': str(obj.get('reasoning') or ''),
    }
