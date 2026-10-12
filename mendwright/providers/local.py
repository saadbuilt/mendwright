"""LocalModelProvider: mendwright's own small selector model, served by Ollama on your
machine.

The model does not write an XPath. mendwright reads the captured page itself
(mendwright.candidates): it lists at most MAX_CANDIDATES elements of the step's role and
asks the model which one is the element the step meant, as {"pick": <n>}, or
{"pick": null, "why": "..."} when none is (the element was removed, several fit equally, or
the step moved to another page). mendwright then builds a unique XPath for the picked
element in code, from its id, test id, name, aria-label or a label, text or context
anchor (never a bare position), and returns it with up to three more XPaths for the same
element as fallbacks. The coordinator checks it with the same gate as any proposal.

The request goes to Ollama's /api/chat with temperature 0 and JSON output. Nothing leaves
the machine unless base_url points elsewhere. No SDK is needed: the stdlib urllib
transport is used unless http= is injected (tests do).

When the gate refuses a pick, the coordinator asks once more. The provider then asks the
model again without that element only when the reason says the element was not uniquely
present or not visible on the live page (a hidden copy, say); for any other reason it does
not guess a second time, so a wrong first pick cannot turn into a wrong second one.

The bundle this provider gets also carries the key's saved fingerprint (the element the
key matched last time), because it sets wants_fingerprint; the coordinator leaves the
fingerprint out for every other provider.

The model is set with model= or MENDWRIGHT_LOCAL_MODEL (default 'mendwright-selector'), and
the host with base_url= or OLLAMA_HOST (default http://localhost:11434). training/ in the
repository has the recipe that builds the model.
"""

import os

from ..candidates import MAX_CANDIDATES, WHY, extract, format_task, parse_answer, robust_xpaths
from ..gate import NOT_LIVE
from ._errors import NOT_HERE_HINT, Describer, clip, sentence

DEFAULT_MODEL = 'mendwright-selector'
DEFAULT_BASE_URL = 'http://localhost:11434'

# Reasons from the gate after which the provider asks again without the refused element.
_RETRY_REASONS = (NOT_LIVE, 'not unique')


def _host(value):
    """OLLAMA_HOST is often written without a scheme ('localhost:11434', '0.0.0.0'), and
    an OpenAI-style base URL ends in /v1; both become a plain http://host:port."""
    text = (value or '').strip().rstrip('/')
    if not text:
        return DEFAULT_BASE_URL
    if '://' not in text:
        text = 'http://' + text
    if text.endswith('/v1'):
        text = text[:-3]
    host = text.split('://', 1)[1]
    if host.startswith('0.0.0.0'):
        text = text.replace('0.0.0.0', '127.0.0.1', 1)
    if ':' not in host.split('/', 1)[0]:
        text += ':11434'
    return text.rstrip('/')


class LocalModelProvider:
    """Ask mendwright's local selector model (in Ollama) which candidate element a broken
    step meant, and turn its pick into a validated-shape proposal."""

    # The coordinator adds the key's saved fingerprint to the bundle for this provider only:
    # the model runs on this machine, and the fingerprint is what it was trained to compare.
    wants_fingerprint = True

    def __init__(self, model=None, base_url=None, *, http=None, timeout=60, num_ctx=4096,
                 keep_alive='10m', max_candidates=MAX_CANDIDATES):
        self.model = model or os.environ.get('MENDWRIGHT_LOCAL_MODEL') or DEFAULT_MODEL
        self.base_url = _host(base_url or os.environ.get('OLLAMA_HOST'))
        self.http = http
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.max_candidates = max_candidates
        self.last_error = None
        # What the model answered last time: {'pick', 'why', 'candidates'}, for logs and tests.
        self.last_answer = None

    # --- HealProvider -----------------------------------------------------------------------

    def propose(self, bundle):
        """Never raises: every failure returns None and leaves a reason in last_error."""
        self.last_error = None
        self.last_answer = None
        try:
            cands = extract(bundle, limit=self.max_candidates)
        except Exception as exc:
            self.last_error = sentence('could not read the captured page ('
                                       + clip(type(exc).__name__ + ': ' + str(exc), 120) + ')')
            return None
        rejected = bundle.get('rejectedXpath')
        if rejected:
            reason = str(bundle.get('rejectReason') or '')
            if not reason.startswith(_RETRY_REASONS):
                self.last_error = sentence(
                    "the local model's pick was refused by mendwright's checks ("
                    + clip(reason, 120) + "), and it does not guess a second time")
                return None
            cands = self._without(cands, rejected)
            if cands is None:
                self.last_error = sentence(
                    "the local model's pick was refused by mendwright's checks ("
                    + clip(reason, 120) + '), and it could not be told apart from the others')
                return None
        if not cands:
            self.last_error = sentence(
                'the local model says no element on this page fits: the page has no '
                + str(bundle.get('expectedRole') or 'matching') + ' element', NOT_HERE_HINT)
            return None
        system, user = format_task(bundle, cands)
        content = self._chat(system, user)
        if content is None:
            return None
        answer = parse_answer(content)
        if answer is None:
            self.last_error = sentence('the local model reply did not say which element it '
                                       'picked: ' + clip(content, 80), self._hints()['reply'])
            return None
        self.last_answer = {'pick': answer['pick'], 'why': answer['why'], 'candidates': len(cands)}
        if answer['pick'] is None:
            why = answer['why'] or 'removed'
            self.last_error = sentence('the local model says no element on this page fits: '
                                       + WHY.get(why, clip(why, 80)), NOT_HERE_HINT)
            return None
        number = answer['pick']
        if number > len(cands):
            self.last_error = sentence('the local model picked candidate ' + str(number)
                                       + ', but there are only ' + str(len(cands)),
                                       self._hints()['reply'])
            return None
        cand = cands[number - 1]
        xpaths = robust_xpaths(cand.element, cands.doc, limit=4)
        if not xpaths:
            self.last_error = sentence('the local model picked ' + clip(cand.line, 100)
                                       + ', but mendwright could not build a unique XPath for it '
                                       'that does not depend on its position')
            return None
        return {'xpath': xpaths[0], 'fallbacks': xpaths[1:],
                'reasoning': 'local model picked candidate ' + str(number) + ' of '
                             + str(len(cands)) + ': ' + clip(cand.line, 160)}

    def report_outcome(self, key, xpath, success):
        """Nothing to report to: the outcome is kept by the coordinator and the store."""
        return None

    # --- helpers ------------------------------------------------------------------------------

    @staticmethod
    def _without(cands, xpath):
        """cands without the element xpath selects, renumbered; None when xpath does not
        select exactly one listed element."""
        from ..candidates import Candidate, Candidates
        try:
            hits = [h for h in cands.doc.xpath(xpath) if isinstance(getattr(h, 'tag', None), str)]
        except Exception:
            return None
        if len(hits) != 1 or cands.number_of(hits[0]) is None:
            return None
        kept = [c for c in cands if c.element is not hits[0]]
        items = [Candidate(n, c.element, c.features, c.line) for n, c in enumerate(kept, start=1)]
        return Candidates(items, doc=cands.doc, role=cands.role, total=max(0, cands.total - 1))

    def _hints(self):
        return {
            'model': "Create it with 'ollama create " + self.model + " -f Modelfile' (see "
                     'training/README.md in the mendwright repository), or set '
                     'MENDWRIGHT_LOCAL_MODEL to the name you gave it.',
            'refused': "Is Ollama running? Start it with 'ollama serve', or set OLLAMA_HOST "
                       '(or base_url=) to where it listens.',
            'connect': 'Check OLLAMA_HOST (or base_url=) and that Ollama is running.',
            'timeout': 'The first call loads the model and can take a while; retry, or pass '
                       'a larger timeout= to LocalModelProvider.',
            'reply': 'Check that MENDWRIGHT_LOCAL_MODEL names the mendwright selector model, '
                     'not a general chat model.',
        }

    def _errors(self):
        return Describer('Ollama', url=self.base_url, hints=self._hints())

    def _chat(self, system, user):
        """The reply text, or None with last_error set."""
        body = {'model': self.model, 'stream': False, 'format': 'json',
                'messages': [{'role': 'system', 'content': system},
                             {'role': 'user', 'content': user}],
                'options': {'temperature': 0, 'num_ctx': self.num_ctx, 'num_predict': 24}}
        if self.keep_alive is not None:
            body['keep_alive'] = self.keep_alive
        http = self.http
        if http is None:
            from ..client import _UrllibHttp
            http = _UrllibHttp()
        try:
            resp = http.post(self.base_url + '/api/chat', json=body,
                             headers={'content-type': 'application/json'}, timeout=self.timeout)
        except Exception as exc:
            self.last_error = self._errors().exception(exc)
            return None
        if getattr(resp, 'status_code', 0) != 200:
            self.last_error = self._errors().response(resp)
            return None
        try:
            data = resp.json()
        except Exception:
            self.last_error = self._errors().bad_body()
            return None
        message = data.get('message') if isinstance(data, dict) else None
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            self.last_error = self._errors().no_text()
            return None
        return content
