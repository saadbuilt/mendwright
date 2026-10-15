"""Canonical heal prompt: build the model request from a capture bundle, and parse the
model's reply into a proposal dict {xpath, fallbacks, reasoning}.

The model may also answer {"xpath": null, "reason": "..."} when no element on the page is
the one the step means (the step moved to another page, or the element was removed).

A bundle for a list heal ({'list': True, ..., 'groups': [...]}, see mendwright.lists) gets
build_list_prompt() instead: the model picks one of the repeated groups of the page by its
xpath, or says that none is the list. The reply is parsed the same way.
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
    if bundle.get('list'):
        return build_list_prompt(bundle)
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


def build_list_prompt(bundle):
    """Return {'system', 'user'} for a list heal: the bundle's groups, each with its xpath, how
    many items it has, its container and its first items. Pure function of the bundle."""
    role = bundle.get('expectedRole')
    system = (
        'You repair ONE broken web-automation selector for a LIST of elements. On an earlier '
        'run it matched every item of a list, such as the cards of a product grid, and now it '
        'matches nothing. Below are the groups of elements that repeat on the page now, each '
        'with its xpath. Reply with ONLY a JSON object: {"xpath":"...","reasoning":"..."}, '
        'where xpath is the xpath of the group whose items are the items of that list, copied '
        "exactly. Each item's role must be '" + str(role) + "'. If no group is that list, for "
        'example because the list is empty now or moved to another page, reply '
        '{"xpath":null,"reason":"..."} instead. Do not pick a group that is only similar, '
        'such as another list elsewhere on the page.'
    )
    lines = []
    for number, group in enumerate(bundle.get('groups') or [], 1):
        if not isinstance(group, dict):
            continue
        lines.append('[' + str(number) + '] ' + str(group.get('xpath')))
        lines.append('    ' + str(group.get('count')) + ' items in ' + str(group.get('container')))
        for item in group.get('items') or []:
            lines.append('    item: ' + str(item))
    user = (
        'key: ' + str(bundle.get('key')) +
        '\nitemRole: ' + str(role) +
        (('\nintent: ' + str(bundle.get('intent'))) if bundle.get('intent') else '') +
        (('\npage: ' + str(bundle.get('page'))) if bundle.get('page') else '') +
        (('\nlast found on page: ' + str(bundle.get('lastPage'))) if bundle.get('lastPage') else '') +
        (('\nitems when it last matched: ' + str(bundle.get('count'))) if bundle.get('count') else '') +
        '\nbroken candidates:\n' + '\n'.join(bundle.get('candidates') or []) +
        (('\nYour previous proposal ' + str(bundle.get('rejectedXpath')) + ' was rejected: '
          + str(bundle.get('rejectReason')) + '. Pick a DIFFERENT group.')
         if bundle.get('rejectReason') else '') +
        '\nrepeated groups (redacted):\n' + '\n'.join(lines)[:_MAX]
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
