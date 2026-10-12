"""mendwright accuracy benchmark: how often a heal lands on the right element, and how
often mendwright accepts a wrong one.

Each case in benchmarks/cases/<id>/ holds a page before a redesign (v1.html), the same
page after it (v2.html), the selector that matched on v1, and an XPath for the element
that is right on v2, or null when the right answer is not to heal at all. A case may give
each version its own address (v1_url, v2_url): the 'flow' cases do, because there the step
moved to another page.

A case runs the way a test suite would meet the redesign:

1. mendwright.open(page) on v1, with a fresh store, and heal.locator(key, selector) so
   the selector matches and its role is learned.
2. The same Healer on v2, where the selector no longer matches. heal.locator() then
   runs the real coordinator: the fingerprint tier first (it recorded the element on v1),
   then the provider, with the real gate and live check.
3. The healed selector is graded by element identity: it must reach the same element
   of the v2 document as the case's truth XPath.

Usage:

    python benchmarks/run.py --provider none                    # the fingerprint tier alone
    python benchmarks/run.py --provider similarity
    python benchmarks/run.py --provider anthropic --model <model id>
    python benchmarks/run.py --provider ollama --model <model> --cases "near-dup-*"
    python benchmarks/run.py --provider similarity --no-fingerprint

Providers: anthropic, openai, gemini, ollama and auto call a model (keys and endpoints
come from the environment, the same way mendwright.open() reads them). similarity and
none need no model. mendwright's fingerprint tier runs before the provider, as it does by
default in mendwright.open(), so --provider none measures the fingerprint tier alone;
--no-fingerprint turns it off to measure the provider alone. The page is a StandInPage
(lxml, no browser) unless --browser picks a headless Playwright browser. Only the
standard library and mendwright are needed; --browser also needs Playwright.

See docs/benchmark.md for what is measured and how to read the results.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import difflib
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import lxml.etree
import lxml.html

import mendwright
from mendwright.capture import redact
from mendwright.demo import StandInPage
from mendwright.exceptions import AttentionRequired
from mendwright.prompt import build_prompt
from mendwright.validation import _role_of

HERE = Path(__file__).resolve().parent
CASES_DIR = HERE / 'cases'
RESULTS_DIR = HERE / 'results'

# Results files written by this harness carry this number. report.py reads it.
FORMAT = 1

CATEGORIES = {
    'id_renamed': 'id renamed or removed',
    'hashed_class': 'hashed class churn',
    'wrapped': 'wrapped in a new container or custom element',
    'label_reworded': 'label text reworded',
    'moved': 'moved to another region',
    'attribute_churn': 'attribute churn (name, data-test, aria)',
    'tag_change': 'tag change, same role',
    'near_duplicate': 'near-duplicates where context decides',
    'hidden_duplicate': 'hidden duplicate next to the visible one',
    'translated': 'translated text',
    'field_order': 'form fields reordered',
    'removed': 'negative: element removed',
    'ambiguous': 'negative: duplicates with nothing to tell them apart',
    'removed_near_danger': 'negative: removed, with a dangerous control nearby',
    'flow': 'negative: the step moved to another page',
}

# Outcomes. A positive case (truth is an XPath) is correct, wrong or missed. A negative
# case (truth is null) is declined or wrong_negative. error is a harness or corpus fault.
OUTCOMES = ('correct', 'wrong', 'missed', 'declined', 'wrong_negative', 'error')

MODEL_PROVIDERS = ('anthropic', 'openai', 'gemini', 'ollama', 'auto')
BASELINES = ('similarity', 'none')
PROVIDERS = MODEL_PROVIDERS + BASELINES
BROWSERS = ('chromium', 'firefox', 'webkit')

# How long a selector is polled before it counts as broken. The pages are static, so
# this only has to cover rendering in a real browser.
STANDIN_TIMEOUT_MS = 0
BROWSER_TIMEOUT_MS = 250
BROWSER_POLL_MS = 50

# Characters per token, for the token estimate. A rough rule for English text and markup;
# the real count depends on the model's tokenizer.
CHARS_PER_TOKEN = 4

_CSS_ID = re.compile(r'^#([A-Za-z_][\w-]*)$')
_XPATH_SPACE = re.compile(r'[ \t\r\n]+')   # what XPath's normalize-space() collapses


# --- cases ------------------------------------------------------------------------------

@dataclass
class Case:
    id: str
    category: str
    description: str
    key: str
    selector: str
    truth: str | None
    v1: str
    v2: str
    role: str | None = None
    destructive: bool = False
    v1_url: str | None = None   # where each version is shown; None: about:blank
    v2_url: str | None = None

    @property
    def positive(self):
        """True when the right answer is a heal, False when it is to decline."""
        return self.truth is not None


def load_case(folder):
    folder = Path(folder)
    data = json.loads((folder / 'case.json').read_text(encoding='utf-8'))
    return Case(id=data['id'], category=data['category'], description=data['description'],
                key=data['key'], selector=data['selector'], truth=data.get('truth'),
                role=data.get('role'), destructive=bool(data.get('destructive')),
                v1_url=data.get('v1_url'), v2_url=data.get('v2_url'),
                v1=(folder / 'v1.html').read_text(encoding='utf-8'),
                v2=(folder / 'v2.html').read_text(encoding='utf-8'))


def load_cases(pattern='*', root=CASES_DIR):
    """Every case whose id matches the glob ``pattern``, sorted by id."""
    root = Path(root)
    folders = sorted(p for p in root.iterdir() if (p / 'case.json').is_file())
    return [load_case(p) for p in folders if fnmatch.fnmatch(p.name, pattern)]


def corpus_hash(cases):
    """A short hash of the cases' files, so a results file names the corpus it ran on."""
    digest = hashlib.sha256()
    for case in sorted(cases, key=lambda c: c.id):
        fields = {'id': case.id, 'category': case.category, 'key': case.key,
                  'selector': case.selector, 'truth': case.truth, 'role': case.role,
                  'destructive': case.destructive}
        if case.v1_url or case.v2_url:
            fields.update(v1_url=case.v1_url, v2_url=case.v2_url)
        meta = json.dumps(fields, sort_keys=True)
        for part in (meta, case.v1, case.v2):
            digest.update(part.replace('\r\n', '\n').encode('utf-8'))
            digest.update(b'\0')
    return digest.hexdigest()[:12]


# --- documents --------------------------------------------------------------------------

def parse(html):
    """The document the way StandInPage parses it."""
    return lxml.html.document_fromstring(html)


def xpath_of(selector):
    """The XPath for a case or heal selector: an XPath (with or without 'xpath=') or a
    '#id' CSS selector, which becomes //*[@id='...']. Anything else comes back as it is,
    and lxml rejects it when it runs."""
    text = (selector or '').strip()
    if text.startswith('xpath='):
        return text[len('xpath='):].strip()
    m = _CSS_ID.match(text)
    if m:
        return "//*[@id='" + m.group(1) + "']"
    return text


def select(doc, selector):
    """The elements ``selector`` matches in ``doc``. [] when it cannot run."""
    try:
        found = doc.xpath(xpath_of(selector))
    except (lxml.etree.XPathError, ValueError, TypeError):
        return []
    if not isinstance(found, list):
        return []
    return [e for e in found if isinstance(getattr(e, 'tag', None), str)]


def element_path(el):
    return el.getroottree().getpath(el)


def is_hidden(el):
    """StandInPage's rule: el or an ancestor has display:none, visibility:hidden or the
    hidden attribute, or el is an input of type hidden."""
    if el.tag == 'input' and (el.get('type') or '').lower() == 'hidden':
        return True
    while el is not None:
        style = (el.get('style') or '').replace(' ', '').lower()
        if 'display:none' in style or 'visibility:hidden' in style:
            return True
        if el.get('hidden') is not None:
            return True
        el = el.getparent()
    return False


def role_of(el):
    """The role mendwright's gate gives an element."""
    return _role_of(el)


def text_of(el):
    """An element's text the way XPath's normalize-space() sees it."""
    return _XPATH_SPACE.sub(' ', el.text_content() or '').strip()


def xpath_literal(value):
    """``value`` as an XPath 1.0 string literal."""
    if "'" not in value:
        return "'" + value + "'"
    if '"' not in value:
        return '"' + value + '"'
    return 'concat(' + ', "\'", '.join("'" + part + "'" for part in value.split("'")) + ')'


# --- XPath for one element ----------------------------------------------------------------

# Attributes worth anchoring on, best first. value is never used: the page mendwright
# sends to a model has its value attributes removed, and so does this baseline's view.
STABLE_ATTRS = ('id', 'data-testid', 'data-test', 'data-qa', 'data-cy', 'name', 'aria-label',
                'placeholder', 'title', 'for', 'autocomplete', 'href', 'type', 'role')
_ANCHOR_ATTRS = ('id', 'data-testid', 'data-test', 'aria-label', 'aria-labelledby', 'name')
_CONTAINERS = ('tr', 'li', 'article', 'section', 'fieldset', 'form', 'div', 'aside', 'nav',
               'header', 'footer', 'main', 'dialog', 'table', 'tbody', 'ul', 'ol')


def _usable(value):
    return (isinstance(value, str) and value.strip() and len(value) <= 120
            and '[URL]' not in value and '[redacted' not in value)


def _own_predicates(el):
    preds = []
    for name in STABLE_ATTRS:
        value = el.get(name)
        if _usable(value):
            preds.append('@' + name + '=' + xpath_literal(value))
    text = text_of(el)
    if 0 < len(text) <= 80 and '[URL]' not in text and '[redacted' not in text:
        preds.append('normalize-space()=' + xpath_literal(text))
    for token in (el.get('class') or '').split()[:4]:
        preds.append("contains(concat(' ', normalize-space(@class), ' '), "
                     + xpath_literal(' ' + token + ' ') + ')')
    return preds


def _anchor_predicates(anc):
    preds = []
    for name in _ANCHOR_ATTRS:
        value = anc.get(name)
        if _usable(value):
            preds.append('@' + name + '=' + xpath_literal(value))
    if anc.tag in _CONTAINERS:
        for probe in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'legend', 'caption', 'td', 'th',
                      'label', 'strong', 'p'):
            hits = anc.xpath('.//' + probe)
            for hit in hits[:1]:
                text = text_of(hit)
                if 0 < len(text) <= 60 and '[URL]' not in text:
                    preds.append('.//' + probe + '[normalize-space()=' + xpath_literal(text) + ']')
    return preds


def robust_xpath(doc, el):
    """An XPath that matches ``el`` and nothing else in ``doc``, built from attributes,
    text and nearby anchors before falling back to a position. Never an index-only path
    (mendwright's gate rejects those)."""
    tag = el.tag

    def unique(xp):
        try:
            hits = [h for h in doc.xpath(xp) if isinstance(getattr(h, 'tag', None), str)]
        except lxml.etree.XPathError:
            return False
        return len(hits) == 1 and hits[0] is el

    own = _own_predicates(el)
    for pred in own:
        xp = '//' + tag + '[' + pred + ']'
        if unique(xp):
            return xp
    for i, first in enumerate(own[:6]):
        for second in own[i + 1:6]:
            xp = '//' + tag + '[' + first + ' and ' + second + ']'
            if unique(xp):
                return xp
    for anc in el.iterancestors():
        if anc.tag in ('html', 'body'):
            break
        for apred in _anchor_predicates(anc):
            for pred in [None] + own[:4]:
                xp = '//' + anc.tag + '[' + apred + ']//' + tag + ('[' + pred + ']' if pred else '')
                if unique(xp):
                    return xp
    base = '//' + tag + ('[' + own[0] + ']' if own else '')
    hits = doc.xpath(base)
    return '(' + base + ')[' + str(hits.index(el) + 1) + ']'


# --- proposers that need no model ---------------------------------------------------------

class NoneProposer:
    """Baseline that never proposes a selector, so every heal is declined. It scores 0 on
    positive cases and declines every negative one, which is the floor any model run
    should beat without giving up the negatives."""

    model = None

    def __init__(self):
        self.last_error = None

    def propose(self, bundle):
        self.last_error = 'the none baseline never proposes a selector.'
        return None

    def report_outcome(self, key, xpath, success):
        pass


def _words(text):
    return set(re.findall(r'[a-z0-9]+', (text or '').lower()))


def _jaccard(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _ratio(a, b):
    return difflib.SequenceMatcher(None, (a or '').lower(), (b or '').lower()).ratio()


def _lcs(a, b):
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            rows[i + 1][j + 1] = rows[i][j] + 1 if x == y else max(rows[i][j + 1], rows[i + 1][j])
    return rows[-1][-1]


def _label_of(el):
    """The text that names a form field: aria-label, a <label for>, a wrapping <label>,
    or the placeholder."""
    parts = [el.get('aria-label') or '']
    ident = el.get('id')
    if ident:
        for label in el.getroottree().getroot().xpath('//label[@for=$i]', i=ident):
            parts.append(text_of(label))
    for anc in el.iterancestors('label'):
        parts.append(text_of(anc))
        break
    parts.append(el.get('placeholder') or '')
    return ' '.join(p for p in parts if p).strip()


_CONTEXT_TAGS = ('tr', 'li', 'article', 'section', 'fieldset', 'form', 'aside', 'nav',
                 'header', 'footer', 'dialog')


def _context_of(el):
    """Words around the element: the text of its nearest row, card, section or form."""
    for anc in el.iterancestors():
        if anc.tag in _CONTEXT_TAGS or anc.get('role') in ('dialog', 'row', 'region'):
            return _words(text_of(anc)[:600])
    return set()


@dataclass
class Fingerprint:
    tag: str
    role: str
    attrs: dict
    classes: set
    text: str
    label: str
    context: set
    ancestors: list


def fingerprint(el):
    attrs = {k: redact(v) for k, v in el.attrib.items() if k not in ('class', 'style', 'value')}
    return Fingerprint(tag=el.tag, role=role_of(el), attrs=attrs,
                       classes=set((el.get('class') or '').split()), text=text_of(el)[:200],
                       label=_label_of(el), context=_context_of(el),
                       ancestors=[a.tag for a in el.iterancestors()])


ATTR_WEIGHTS = {'id': 2.0, 'data-testid': 1.5, 'data-test': 1.5, 'data-qa': 1.5, 'name': 1.5,
                'aria-label': 1.5, 'placeholder': 1.0, 'href': 1.0, 'title': 1.0, 'for': 1.0,
                'autocomplete': 1.0, 'type': 0.5}


def similarity(old, new):
    """0 to 1: how alike two fingerprints are, as a weighted average of tag, attributes,
    classes, text, label, surrounding words and ancestor path."""
    parts = [(1.0, 1.0 if old.tag == new.tag else 0.0)]
    for name, value in old.attrs.items():
        weight = ATTR_WEIGHTS.get(name, 0.5)
        other = new.attrs.get(name)
        parts.append((weight, _ratio(value, other) if other is not None else 0.0))
    if old.classes:
        parts.append((1.0, _jaccard(old.classes, new.classes)))
    if old.text or new.text:
        parts.append((2.0, _ratio(old.text, new.text)))
    if old.label:
        parts.append((2.0, _ratio(old.label, new.label)))
    if old.context:
        parts.append((2.0, _jaccard(old.context, new.context)))
    longest = max(len(old.ancestors), len(new.ancestors), 1)
    parts.append((1.0, _lcs(old.ancestors, new.ancestors) / longest))
    total = sum(w for w, _ in parts)
    return sum(w * s for w, s in parts) / total


class SimilarityProposer:
    """A model-free baseline in the style of Healenium: when the v1 selector matches, it
    records that element (tag, attributes, text, label, surrounding words, ancestor path).
    When mendwright asks for a heal, it scores every element of the expected role on the
    page it is sent, takes the most similar one, and builds an XPath for it. Below
    THRESHOLD it declines. On a re-prompt it skips the element that was just rejected.

    It never sees the case's truth. It sees the v1 page (through learn()), which a model
    does not, and the same redacted v2 page a model gets."""

    THRESHOLD = 0.5
    model = None

    def __init__(self, threshold=None):
        self.threshold = self.THRESHOLD if threshold is None else threshold
        self.last_error = None
        self._known = {}
        self._tried = {}

    def learn(self, key, html, selector):
        """Record the element ``selector`` matches in ``html`` (the page before the
        redesign) for ``key``."""
        hits = select(parse(html), selector)
        self._known[key] = fingerprint(hits[0]) if len(hits) == 1 else None
        self._tried[key] = set()

    def propose(self, bundle):
        self.last_error = None
        key = bundle.get('key')
        old = self._known.get(key)
        if old is None:
            self.last_error = 'the similarity baseline has no record of ' + repr(key) + '.'
            return None
        try:
            doc = parse(bundle.get('html') or '')
        except Exception:
            self.last_error = 'the similarity baseline could not parse the page.'
            return None
        tried = self._tried.setdefault(key, set())
        if bundle.get('rejectedXpath'):
            tried.update(element_path(e) for e in select(doc, bundle['rejectedXpath']))
        want = bundle.get('expectedRole')
        best, best_score = None, -1.0
        for el in doc.iter():
            if not isinstance(el.tag, str) or role_of(el) != want:
                continue
            if element_path(el) in tried:
                continue
            score = similarity(old, fingerprint(el))
            if score > best_score:
                best, best_score = el, score
        if best is None or best_score < self.threshold:
            self.last_error = ('no element on the page is similar enough (best score '
                               + format(max(best_score, 0.0), '.2f') + ', threshold '
                               + format(self.threshold, '.2f') + ').')
            return None
        tried.add(element_path(best))
        return {'xpath': robust_xpath(doc, best), 'fallbacks': [],
                'reasoning': 'similarity ' + format(best_score, '.2f')}

    def report_outcome(self, key, xpath, success):
        pass


class OracleProposer:
    """Proposes the case's truth, and declines when the truth is null. Only the tests use
    it, to check the harness and the grader. Its numbers say nothing about any model and
    are never reported."""

    model = None

    def __init__(self, truth):
        self.truth = truth
        self.last_error = None

    def propose(self, bundle):
        if self.truth is None:
            self.last_error = 'the oracle declines a negative case.'
            return None
        self.last_error = None
        return {'xpath': self.truth, 'fallbacks': [], 'reasoning': 'oracle'}

    def report_outcome(self, key, xpath, success):
        pass


# --- model providers ------------------------------------------------------------------------

def _ollama(model):
    from mendwright import OpenAICompatibleProvider
    model = model or os.environ.get('OLLAMA_MODEL')
    if not model:
        raise ValueError('set --model or OLLAMA_MODEL to the Ollama model to use.')
    kwargs = {'api_key': ''}   # keyless: never send OPENAI_API_KEY to a local host
    host = os.environ.get('OLLAMA_HOST')
    if host:
        base = (host if '://' in host else 'http://' + host).rstrip('/')
        kwargs['base_url'] = base if base.endswith('/v1') else base + '/v1'
    return OpenAICompatibleProvider.for_ollama(model, **kwargs)


def build_provider(name, model=None):
    """The proposer for ``--provider name``. Model providers read their key from the
    environment, as mendwright.open() does; model defaults to the provider's *_MODEL
    variable."""
    if name == 'none':
        return NoneProposer()
    if name == 'similarity':
        return SimilarityProposer()
    if name == 'anthropic':
        return mendwright.AnthropicProvider(model=model)
    if name == 'openai':
        return mendwright.OpenAICompatibleProvider(model=model)
    if name == 'gemini':
        return mendwright.GeminiProvider(model=model)
    if name == 'ollama':
        return _ollama(model)
    if name == 'auto':
        return mendwright.auto_provider(model=model)
    raise ValueError('unknown provider ' + repr(name) + '. Use one of: ' + ', '.join(PROVIDERS))


def endpoint(provider):
    """Where a provider sends requests, as scheme://host[:port]/path, or None."""
    url = getattr(provider, 'base_url', None)
    if not url and type(provider).__name__ == 'GeminiProvider':
        from mendwright.providers import gemini
        url = getattr(gemini, '_API_ROOT', None)
    if not url and type(provider).__name__ == 'GatewayProvider':
        url = getattr(provider, 'gateway', None) or os.environ.get('MENDWRIGHT_GATEWAY')
    if not isinstance(url, str) or not url.strip():
        return None
    from urllib.parse import urlsplit
    parts = urlsplit(url if '://' in url else 'https://' + url)
    if not parts.hostname:
        return None
    port = ':' + str(parts.port) if parts.port else ''
    return parts.scheme + '://' + parts.hostname + port + parts.path.rstrip('/')


# Variables that change where a provider sends requests. The run names the ones that are
# set, so a proxy configured for another tool does not receive the pages unnoticed.
ENDPOINT_VARS = {
    'anthropic': ('ANTHROPIC_BASE_URL',),
    'openai': ('OPENAI_BASE_URL',),
    'ollama': ('OLLAMA_HOST',),
    'auto': ('MENDWRIGHT_PROVIDER', 'ANTHROPIC_BASE_URL', 'OPENAI_BASE_URL', 'MENDWRIGHT_GATEWAY',
             'OLLAMA_HOST'),
}

_SECRET_WORDS = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'AUTH')


def scrub(text):
    """``text`` with the value of every secret-looking environment variable replaced."""
    if not isinstance(text, str):
        return text
    for name, value in os.environ.items():
        if value and len(value) >= 4 and any(w in name.upper() for w in _SECRET_WORDS):
            text = text.replace(value, '***')
    return text


# --- one case ---------------------------------------------------------------------------------

class Recorder:
    """Wraps a provider's propose() on the instance to count calls, time them and measure
    the prompt each call would send (mendwright.prompt.build_prompt). The coordinator still
    sees the provider's own class, model and last_error."""

    def __init__(self, provider):
        self.provider = provider
        self.calls = []
        self._own = 'propose' in vars(provider)
        real = provider.propose

        def propose(bundle):
            prompt = build_prompt(bundle)
            chars = len(prompt['system']) + len(prompt['user'])
            started = time.perf_counter()
            try:
                return real(bundle)
            finally:
                self.calls.append({'chars': chars,
                                   'ms': (time.perf_counter() - started) * 1000.0})

        self._real = real
        provider.propose = propose

    def remove(self):
        if self._own:
            self.provider.propose = self._real
        else:
            with contextlib.suppress(AttributeError):
                del self.provider.propose


@contextlib.contextmanager
def _healing_on():
    """MENDWRIGHT_DISABLE=1 turns healing off even with provider= passed. A benchmark run
    is meant to heal, so the variable is set aside while the Healer is opened."""
    saved = os.environ.pop('MENDWRIGHT_DISABLE', None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ['MENDWRIGHT_DISABLE'] = saved


def _journal(folder):
    path = Path(folder) / 'journal.jsonl'
    events = []
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            with contextlib.suppress(ValueError):
                events.append(json.loads(line))
    return events


def _target(doc, xpath, truth_path):
    """What an XPath reaches in the graded document: 'truth', 'other', 'none' or 'several'."""
    hits = select(doc, xpath)
    if not hits:
        return 'none'
    if len(hits) > 1:
        return 'several'
    return 'truth' if element_path(hits[0]) == truth_path else 'other'


def _provider_error(reason, provider):
    """True when a decline came from a failed call (network, HTTP, auth) rather than from
    the model's answer or a gate check."""
    error = getattr(provider, 'last_error', None)
    if not isinstance(error, str) or not error or error not in (reason or ''):
        return False
    if isinstance(provider, (NoneProposer, SimilarityProposer, OracleProposer)):
        return False
    return not error.lower().startswith('the model')


def show(page, html, url=None):
    """Load ``html`` into ``page``. With a url, the page reports that address: a
    StandInPage gets it set, and a Playwright page navigates to it, with a route that
    answers from ``html`` (and with an empty 404 for anything else), so nothing is
    fetched from the network."""
    if url is None:
        page.set_content(html)
        return
    if isinstance(page, StandInPage):
        page.url = url
        page.set_content(html)
        return
    served = getattr(page, '_bench_served', None)
    if served is None:
        served = page._bench_served = {}

        def answer(route):
            body = served.get(route.request.url.split('#', 1)[0])
            if body is None:
                route.fulfill(status=404, body='')
            else:
                route.fulfill(status=200, content_type='text/html', body=body)
        page.route('**/*', answer)
    served[url] = html
    page.goto(url)


def run_case(case, proposer, page, *, timeout_ms=STANDIN_TIMEOUT_MS, poll_ms=0, run=1,
             heal_across_pages=False, fingerprint=True):
    """Run one case on ``page`` (a StandInPage or a sync Playwright page) with
    ``proposer`` and return its record. The store lives in a temporary folder.
    heal_across_pages and fingerprint are passed to mendwright.open(); the published runs
    leave heal_across_pages off."""
    folder = tempfile.mkdtemp(prefix='mendwright-bench-')
    recorder = Recorder(proposer)
    record = {'id': case.id, 'category': case.category, 'run': run,
              'expected': 'heal' if case.positive else 'decline', 'outcome': 'error',
              'healed_xpath': None, 'healed_target': None, 'learned_role': None,
              'model_calls': 0, 'model_ms': 0.0, 'heal_ms': 0.0, 'prompt_chars': 0,
              'prompt_chars_total': 0, 'est_tokens': 0, 'decline_reason': None,
              'provider_error': False, 'proposals': [], 'gate_caught_wrong': 0, 'note': None,
              'healed_by': None}
    try:
        with _healing_on():
            heal = mendwright.open(page, provider=proposer,
                                   store_path=os.path.join(folder, 'overrides.json'),
                                   dry_run=False, timeout_ms=timeout_ms,
                                   rate_per_key_per_hour=None, rate_global_per_hour=None,
                                   heal_across_pages=heal_across_pages,
                                   fingerprint=fingerprint)
        log = []
        heal.coordinator.log = log.append
        kwargs = {'poll_ms': poll_ms}
        if case.role:
            kwargs['role'] = case.role
        if case.destructive:
            kwargs['destructive'] = True

        show(page, case.v1, case.v1_url)
        try:
            heal.locator(case.key, case.selector, **kwargs)
        except AttentionRequired as exc:
            record['note'] = 'the selector did not match v1: ' + scrub(str(exc)).splitlines()[0]
            return record
        record['learned_role'] = heal.store.meta(case.key).get('expectedRole')
        learn = getattr(proposer, 'learn', None)
        if callable(learn):
            learn(case.key, page.content(), case.selector)

        show(page, case.v2, case.v2_url)
        started = time.perf_counter()
        healed, reason = None, None
        try:
            heal.locator(case.key, case.selector, **kwargs)
            if not heal.heals:
                record['note'] = 'the v1 selector still matches v2, so nothing was healed'
                return record
            healed = heal.heals[-1].new
        except AttentionRequired as exc:
            reason = heal.coordinator.explain(case.key) or str(exc)
        record['heal_ms'] = round((time.perf_counter() - started) * 1000.0, 1)

        doc = parse(page.content())
        truth_path = None
        if case.truth is not None:
            hits = select(doc, case.truth)
            if len(hits) != 1:
                record['note'] = ('the truth XPath matches ' + str(len(hits))
                                  + ' elements on the page, so the case cannot be graded')
                return record
            truth_path = element_path(hits[0])

        record['healed_xpath'] = healed
        record['decline_reason'] = scrub(reason) if reason else None
        record['provider_error'] = bool(reason) and _provider_error(reason, proposer)
        if healed is not None:
            record['healed_target'] = _target(doc, healed, truth_path)
        if case.positive:
            if healed is None:
                record['outcome'] = 'missed'
            else:
                record['outcome'] = 'correct' if record['healed_target'] == 'truth' else 'wrong'
        else:
            record['outcome'] = 'declined' if healed is None else 'wrong_negative'

        rejected = {}
        for event in _journal(folder):
            if event.get('event') == 'heal.rejected' and event.get('xpath'):
                rejected[event['xpath']] = scrub(str(event.get('reason') or ''))
            if event.get('event') == 'heal.applied' and healed is not None:
                record['healed_by'] = ('fingerprint' if event.get('provider') == 'fingerprint'
                                       else 'model')
        for event in _journal(folder):
            if event.get('event') != 'heal.proposed':
                continue
            xpath = str(event.get('xpath') or '')
            target = _target(doc, xpath, truth_path) if xpath else 'none'
            if healed is not None and xpath and xpath_of(xpath) == xpath_of(healed):
                verdict = 'applied'
            else:
                verdict = rejected.get(xpath, 'not applied')
            record['proposals'].append({'xpath': scrub(xpath), 'result': verdict, 'target': target,
                                        'source': event.get('source') or 'model'})
            if verdict != 'applied' and target == 'other':
                record['gate_caught_wrong'] += 1
    except Exception as exc:   # a harness or page fault: keep going, and say so
        record['outcome'] = 'error'
        record['note'] = scrub(type(exc).__name__ + ': ' + str(exc)).splitlines()[0]
    finally:
        calls = recorder.calls
        recorder.remove()
        record['model_calls'] = len(calls)
        record['model_ms'] = round(sum(c['ms'] for c in calls), 1)
        record['prompt_chars'] = calls[0]['chars'] if calls else 0
        record['prompt_chars_total'] = sum(c['chars'] for c in calls)
        record['est_tokens'] = round(record['prompt_chars_total'] / CHARS_PER_TOKEN)
        shutil.rmtree(folder, ignore_errors=True)
    return record


# --- many cases ---------------------------------------------------------------------------------

class SetupError(Exception):
    """The run could not start: no Playwright, no browser, or no provider."""


class _FirstCallFailed(Exception):
    """The first case was declined because the provider call itself failed."""


@contextlib.contextmanager
def browser_session(name):
    """A headless Playwright browser that hands out a fresh page per case."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SetupError('--browser needs Playwright: pip install "mendwright[playwright]", '
                         'then python -m playwright install ' + name) from None
    manager = sync_playwright().start()
    try:
        try:
            browser = getattr(manager, name).launch(headless=True)
        except Exception as exc:
            first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            raise SetupError('could not start ' + name + ' (' + first + '). Install it with: '
                             'python -m playwright install ' + name) from None
        try:
            yield browser
        finally:
            browser.close()
    finally:
        manager.stop()


def run_cases(cases, proposer, *, browser=None, repeat=1, progress=None, fingerprint=True):
    """Run every case ``repeat`` times and return the records. ``proposer`` is one
    provider shared by all cases, or a callable that takes a Case and returns one.
    fingerprint=False turns mendwright's fingerprint tier off, so only the provider heals."""
    make = proposer if callable(proposer) and not hasattr(proposer, 'propose') else (lambda case: proposer)
    records = []
    total = len(cases) * repeat
    with contextlib.ExitStack() as stack:
        session = stack.enter_context(browser_session(browser)) if browser else None
        for run in range(1, repeat + 1):
            for case in cases:
                if session is not None:
                    page = session.new_page()
                    try:
                        record = run_case(case, make(case), page, timeout_ms=BROWSER_TIMEOUT_MS,
                                          poll_ms=BROWSER_POLL_MS, run=run,
                                          fingerprint=fingerprint)
                    finally:
                        page.close()
                else:
                    record = run_case(case, make(case), StandInPage(), run=run,
                                      fingerprint=fingerprint)
                records.append(record)
                if progress:
                    progress(len(records), total, record)
    return records


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def summarize(records, repeat=1):
    """Counts and rates over ``records``. Rates are percentages of the positive or the
    negative cases; repeats count as separate attempts."""
    pos = [r for r in records if r['expected'] == 'heal']
    neg = [r for r in records if r['expected'] == 'decline']
    count = {o: sum(1 for r in records if r['outcome'] == o) for o in OUTCOMES}
    called = [r for r in records if r['model_calls']]
    sized = [r for r in records if r['prompt_chars']]
    total_chars = sum(r['prompt_chars_total'] for r in records)
    avg_chars = sum(r['prompt_chars'] for r in sized) / len(sized) if sized else None
    return {
        'cases': len(records), 'positives': len(pos), 'negatives': len(neg),
        'correct': count['correct'], 'wrong': count['wrong'], 'missed': count['missed'],
        'declined': count['declined'], 'wrong_negative': count['wrong_negative'],
        'errors': count['error'],
        'provider_errors': sum(1 for r in records if r['provider_error']),
        'correct_pct': _pct(count['correct'], len(pos)),
        'wrong_pct': _pct(count['wrong'], len(pos)),
        'missed_pct': _pct(count['missed'], len(pos)),
        'declined_pct': _pct(count['declined'], len(neg)),
        'wrong_negative_pct': _pct(count['wrong_negative'], len(neg)),
        'wrong_heals': count['wrong'] + count['wrong_negative'],
        'wrong_heals_pct': _pct(count['wrong'] + count['wrong_negative'], len(records)),
        'gate_caught_wrong': sum(r['gate_caught_wrong'] for r in records),
        'fingerprint_heals': sum(1 for r in records if r.get('healed_by') == 'fingerprint'),
        'avg_model_calls': round(sum(r['model_calls'] for r in records) / len(records), 2) if records else None,
        'avg_model_ms': round(sum(r['model_ms'] for r in called) / len(called), 1) if called else None,
        'avg_prompt_chars': round(avg_chars) if sized else None,
        'avg_prompt_tokens_est': round(avg_chars / CHARS_PER_TOKEN) if sized else None,
        'run_prompt_chars': round(total_chars / max(repeat, 1)),
        'run_tokens_est': round(total_chars / max(repeat, 1) / CHARS_PER_TOKEN),
    }


def by_category(records):
    out = {}
    for name in CATEGORIES:
        rows = [r for r in records if r['category'] == name]
        if rows:
            out[name] = {o: sum(1 for r in rows if r['outcome'] == o) for o in OUTCOMES}
            out[name]['cases'] = len(rows)
    return out


def _fmt_pct(value):
    return '-' if value is None else format(value, '.1f') + '%'


def markdown_summary(result):
    s = result['summary']
    who = result['provider'] + (' (' + result['model'] + ')' if result.get('model') else '')
    lines = [
        '| Provider | Fingerprint | Page | Cases | Correct | Wrong | Missed | Negatives declined '
        '| Wrong on negatives | Healed by fingerprint | Avg model calls | Avg prompt chars (est. tokens) |',
        '|---|---|---|---|---|---|---|---|---|---|---|---|',
        '| ' + ' | '.join([
            who, 'on' if result.get('fingerprint') else 'off', result['page'], str(s['cases']),
            str(s['correct']) + '/' + str(s['positives']) + ' (' + _fmt_pct(s['correct_pct']) + ')',
            str(s['wrong']) + ' (' + _fmt_pct(s['wrong_pct']) + ')',
            str(s['missed']) + ' (' + _fmt_pct(s['missed_pct']) + ')',
            str(s['declined']) + '/' + str(s['negatives']),
            str(s['wrong_negative']),
            str(s.get('fingerprint_heals', 0)),
            str(s['avg_model_calls']),
            (str(s['avg_prompt_chars']) + ' (~' + str(s['avg_prompt_tokens_est']) + ')')
            if s['avg_prompt_chars'] else '-',
        ]) + ' |',
    ]
    return '\n'.join(lines)


def default_out(provider, model, page_mode, when, fingerprint=True):
    """benchmarks/results/<provider>[-<model>][-nofp]-<page>-<date>.json: -nofp marks a run
    with the fingerprint tier off."""
    name = provider + ('-' + re.sub(r'[^A-Za-z0-9._-]+', '_', model) if model else '')
    name += '' if fingerprint else '-nofp'
    return RESULTS_DIR / (name + '-' + page_mode + '-' + when.strftime('%Y%m%d') + '.json')


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python benchmarks/run.py',
        description='Measure how often a heal picks the right element. See docs/benchmark.md.')
    parser.add_argument('--provider', required=True, choices=PROVIDERS,
                        help='who proposes the new selector: a model provider, or one of the '
                             'model-free baselines (similarity, none)')
    parser.add_argument('--model', help="the model id (default: the provider's *_MODEL variable)")
    parser.add_argument('--cases', default='*', help='glob over case ids (default: all)')
    parser.add_argument('--browser', choices=BROWSERS,
                        help='run in a real headless Playwright browser instead of StandInPage')
    parser.add_argument('--out', help='results file '
                                      '(default: benchmarks/results/<provider>-<page>-<date>.json)')
    parser.add_argument('--repeat', type=int, default=1, help='run every case N times (default 1)')
    parser.add_argument('--no-fingerprint', action='store_true',
                        help="turn mendwright's fingerprint tier off, so only the provider heals "
                             '(default: on, as in mendwright.open())')
    args = parser.parse_args(argv)
    fingerprint = not args.no_fingerprint
    if args.repeat < 1:
        parser.error('--repeat must be 1 or more')

    cases = load_cases(args.cases)
    if not cases:
        parser.error('no case id matches ' + repr(args.cases))
    try:
        proposer = build_provider(args.provider, args.model)
    except Exception as exc:
        print('benchmark: could not set up provider ' + args.provider + ': ' + scrub(str(exc)),
              file=sys.stderr)
        return 2

    page_mode = args.browser or 'standin'
    model = getattr(proposer, 'model', None)
    model = model if isinstance(model, str) and model else None
    url = endpoint(proposer)
    print('mendwright ' + mendwright.__version__ + ' benchmark: ' + str(len(cases)) + ' case(s) x '
          + str(args.repeat) + ', page: ' + ('headless ' + args.browser if args.browser else
                                             'StandInPage (lxml, no browser)'))
    if args.provider in BASELINES:
        print('provider: ' + type(proposer).__name__ + ' (model-free baseline, no network)')
        if args.provider == 'none' and fingerprint:
            print('the none baseline never proposes, so this run measures the fingerprint tier alone')
    else:
        print('provider: ' + type(proposer).__name__ + (', model ' + model if model else ''))
        print('endpoint: ' + (url or 'the provider default') + '  (each case sends its v2 page here)')
        for var in ENDPOINT_VARS.get(args.provider, ()):
            if os.environ.get(var):
                print('note: ' + var + ' is set in this environment')
        if args.provider in ('anthropic', 'openai') and not getattr(proposer, 'api_key', None):
            print('note: no API key is set for this provider; calls will fail unless the '
                  'endpoint needs none')
        print('model calls: at most ' + str(2 * len(cases) * args.repeat)
              + ' (2 per case: a proposal and one retry)')
    print('fingerprint tier: ' + ('on' if fingerprint else 'off (--no-fingerprint)'))
    print('', flush=True)

    def progress(done, total, record):
        calls = record['model_calls']
        print('  [' + str(done).rjust(len(str(total))) + '/' + str(total) + '] '
              + record['id'].ljust(44) + ' ' + record['outcome'].ljust(14) + ' '
              + str(calls) + ' call' + ('' if calls == 1 else 's')
              + ('  (fingerprint)' if record.get('healed_by') == 'fingerprint' else '')
              + (('  ' + record['note']) if record['note'] else ''), file=sys.stderr, flush=True)
        if done == 1 and record['provider_error']:
            # A bad key, model id or endpoint fails every case the same way. Stop here
            # rather than spend the whole run on it.
            raise _FirstCallFailed(record['decline_reason'])

    when = datetime.datetime.now(datetime.timezone.utc)
    try:
        records = run_cases(cases, proposer, browser=args.browser, repeat=args.repeat,
                            progress=progress, fingerprint=fingerprint)
    except SetupError as exc:
        print('benchmark: ' + str(exc), file=sys.stderr)
        return 2
    except _FirstCallFailed as exc:
        print('benchmark: the first provider call failed, so the run stopped and no results '
              'were written. ' + str(exc), file=sys.stderr)
        return 2

    result = {
        'format': FORMAT,
        'date': when.replace(microsecond=0).isoformat(),
        'mendwright': mendwright.__version__,
        'python': platform.python_version(),
        'platform': platform.platform(terse=True),
        'provider': args.provider,
        'provider_class': type(proposer).__name__,
        'model': model,
        'fingerprint': fingerprint,
        'endpoint': url,
        'page': page_mode,
        'repeat': args.repeat,
        'cases_glob': args.cases,
        'case_count': len(cases),
        'corpus': corpus_hash(cases),
        'chars_per_token': CHARS_PER_TOKEN,
        'summary': summarize(records, args.repeat),
        'by_category': by_category(records),
        'records': records,
    }
    out = Path(args.out) if args.out else default_out(args.provider, model, page_mode, when,
                                                      fingerprint)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + '\n', encoding='utf-8', newline='\n')

    print(markdown_summary(result))
    s = result['summary']
    print('')
    print('Wrong heals (accepted onto the wrong element): ' + str(s['wrong_heals']) + ' of '
          + str(s['cases']) + '. Wrong proposals the gate stopped: ' + str(s['gate_caught_wrong']) + '.')
    if fingerprint:
        print('Heals by the fingerprint tier (no model call): ' + str(s['fingerprint_heals']) + '.')
    if s['provider_errors']:
        print('Provider errors: ' + str(s['provider_errors']) + ' case(s) were declined because a '
              'call failed (see decline_reason in the results file). Rerun those before '
              'comparing numbers.')
    if s['errors']:
        print('Harness errors: ' + str(s['errors']) + ' case(s); see "note" in the results file.')
    print('Estimated prompt size for one pass over these cases: ' + str(s['run_prompt_chars'])
          + ' characters, about ' + str(s['run_tokens_est']) + ' input tokens (characters / '
          + str(CHARS_PER_TOKEN) + ', an estimate).')
    print('Results: ' + str(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
