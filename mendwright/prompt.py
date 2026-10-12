"""Canonical heal prompt: build the model request from a capture bundle, and parse the
model's reply into a proposal dict {xpath, fallbacks, reasoning}.

The model may also answer {"xpath": null, "reason": "..."} when no element on the page is
the one the step means (the step moved to another page, or the element was removed).
parse_proposal() returns None for that reply, as for any reply without an xpath, and
not_here_reason() returns the model's reason, which the providers put in last_error.

This is the only prompt in mendwright. Every direct provider builds its request from
build_prompt() and parses the reply with parse_proposal(), so the prompt is the same for
Anthropic, OpenAI-compatible APIs, Gemini, Bedrock and LiteLLM. The reference gateway
(examples/heal_gateway.mjs) has a JavaScript copy of these functions. A change here needs
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
        "The xpath must match EXACTLY ONE element whose role is '" + str(expected_role) + "'. "
        'If no element on this page is the one the step means, for example because the step '
        'moved to another page or the element was removed, reply {"xpath":null,"reason":"..."} '
        'instead. Do not pick an element that is only similar.'
    )
    if destructive:
        system += (' This is a DESTRUCTIVE control: its exact visible text MUST be one of '
                   + json.dumps(expected_text) + '.')
    user = (
        'key: ' + str(bundle.get('key')) +
        '\nexpectedRole: ' + str(expected_role) +
        '\nexpectedText: ' + json.dumps(expected_text) +
        (('\nintent: ' + str(bundle.get('intent'))) if bundle.get('intent') else '') +
        '\ndestructive: ' + ('true' if destructive else 'false') +
        (('\npage: ' + str(bundle.get('page'))) if bundle.get('page') else '') +
        (('\nlast found on page: ' + str(bundle.get('lastPage'))) if bundle.get('lastPage') else '') +
        '\nbroken candidates:\n' + '\n'.join(bundle.get('candidates') or []) +
        (('\nYour previous proposal ' + str(bundle.get('rejectedXpath')) + ' was rejected: '
          + str(bundle.get('rejectReason')) + '. Propose a DIFFERENT selector.')
         if bundle.get('rejectReason') else '') +
        '\nDOM inventory (redacted):\n' + json.dumps(bundle.get('inventory') or [])[:_MAX] +
        '\nHTML snapshot (redacted):\n' + str(bundle.get('html') or '')[:_MAX]
    )
    return {'system': system, 'user': user}


def _reply_object(text):
    """The JSON object in a model reply (text around it is allowed), or None."""
    if not text:
        return None
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def parse_proposal(text):
    """Extract {xpath, fallbacks, reasoning} from a model reply, or None. Tolerates text
    around the JSON object. Returns None unless a non-empty xpath is present, so a reply
    saying no element fits ({"xpath": null, "reason": "..."}) is None too; see
    not_here_reason()."""
    obj = _reply_object(text)
    if obj is None or not obj.get('xpath'):
        return None
    fallbacks = obj.get('fallbacks')
    return {
        'xpath': str(obj['xpath']),
        'fallbacks': [str(f) for f in fallbacks[:4]] if isinstance(fallbacks, list) else [],
        'reasoning': str(obj.get('reasoning') or ''),
    }


def not_here_reason(text):
    """The model's reason when its reply says no element on the page fits the step: a
    JSON object without an xpath (null, empty or left out) and with a non-empty "reason"
    string. None for any other reply."""
    obj = _reply_object(text)
    if obj is None or obj.get('xpath'):
        return None
    reason = obj.get('reason')
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return None
