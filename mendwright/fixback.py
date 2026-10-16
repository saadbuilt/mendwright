"""`mendwright fix`: put healed selectors back into the code that passes the old ones.

A heal lives in the override store, and your code still says the old selector, so the next
run heals again from the store. This module finds the keys whose best selector is a heal,
finds the old selector as a string literal in your source files, and rewrites that literal.
Once the code holds the new selector, mendwright's "code wins" rule makes it the first
candidate on the next run.

What it will and will not touch:

- Only keys whose most recently successful active selector came from a heal, and (unless
  asked) only heals that journal.jsonl shows a canary success for (the pytest plugin writes
  one when the test that made the heal passes, and an expect= or confirm(True) check does too).
- A CSS selector in your code gets the heal's CSS form (mendwright.css) when it has one;
  otherwise the healed XPath.
- Never a key marked destructive (in the store, or with --destructive-key), never a heal that
  sits inside an iframe, never a heal mendwright could not have saved (an XPath lxml cannot
  compile, or a CSS form mendwright.css does not build), and never a key whose selectors
  came only from a seed file (there is no string in your code to replace).
- Never the selector of heal.interrupt() or the landmark of heal.step(when=): equal to the
  old selector of a healed key, it is listed instead, since another key's heal there would
  make the interruption act on that key's element. A string that reaches the call through a
  name in the same file counts too (_FlowTrace); one from another module does not.
- Only a string literal whose whole value is the old selector (compared as stored, so
  "xpath=//a" and " //a " both match the stored "//a"). Not a substring, not a comment,
  not an f-string, not bytes, not part of an implicit concatenation.
- Never a file in which the same string belongs to more than one key: it is left as it is
  and listed, because mendwright cannot tell which new selector goes where.

Python files are read with tokenize and checked with ast: after the rewrite the file must
still parse, have the same structure, and differ only in the values of the replaced
strings. Other files (--glob '*.ts', '*.robot') are scanned line by line for quoted strings.
Bytes, a BOM and CRLF line endings are kept, and a file is replaced atomically.
"""

from __future__ import annotations

import ast
import codecs
import difflib
import fnmatch
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import time
import tokenize
from dataclasses import dataclass, field

from . import _md
from .selectors import CSS, XPATH, check_syntax, classify, normalize

# Folders that never hold your code. A folder is skipped by name, at any depth. .github holds
# workflows and local actions, which a heal must never change.
SKIP_DIRS = frozenset({
    '.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '.mendwright', 'build', 'dist',
    '__pycache__', '.tox', '.nox', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.eggs',
    'site-packages', '.claude', '.github',
})
# A folder never scanned even when --path names it or a folder inside it, in any case or
# through a link (see _in_github).
NEVER_SCANNED = '.github'
DEFAULT_GLOBS = ('*.py',)
MAX_FILE_BYTES = 2 * 1024 * 1024        # larger files are not source code worth scanning

PYTHON_EXTS = ('.py', '.pyw', '.pyi')
# Languages with // and /* */ comments, where \' \" \\ escapes work in both quote styles.
C_LIKE_EXTS = frozenset({'.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts', '.java',
                         '.kt', '.kts', '.cs', '.groovy', '.scala', '.php', '.swift'})
ESCAPING_EXTS = C_LIKE_EXTS | {'.rb'}
# Languages in which only double quotes make a string ('x' is a char or a rune, or not
# allowed). A selector with a double quote in it is escaped there, never re-quoted.
DOUBLE_QUOTE_ONLY_EXTS = frozenset({'.java', '.kt', '.kts', '.cs', '.scala', '.swift', '.go',
                                    '.rs', '.json', '.c', '.h', '.cc', '.cpp', '.hpp', '.m'})

_NEEDLE_SPLIT = re.compile(r'''['"\\\s]''')
_LINE = re.compile(r'[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+')
_WRITE_TRIES = 8

# An old selector that is one word ('button', 'input') equals strings that are not
# selectors (role='button', a dict entry), so a key with one is never written back.
_BARE_WORD = re.compile(r'^[A-Za-z][\w-]*$')
# Keys that heal.driver makes for driver.find_element(by, value), and that By. The healed
# selector is an XPath, which these Bys cannot run.
_DRIVER_KEYS = (('css selector=', 'By.CSS_SELECTOR'), ('id=', 'By.ID'), ('name=', 'By.NAME'))

# The CSS forms mendwright.css builds for a heal: a compound of a tag name, #id, .class and
# [attribute="value"] (css_string's quoting), with at most one ancestor compound before it.
_CSS_IDENT = r'-?[A-Za-z_][A-Za-z0-9_-]*'
_CSS_NAME = r'[A-Za-z_][A-Za-z0-9_:.-]*'
_CSS_STRING = r'"(?:[^"\\\r\n\f]|\\["\\]|\\[0-9a-f]{1,6} )*"'
_CSS_PART = r'(?:\#' + _CSS_IDENT + r'|\.' + _CSS_IDENT + r'|\[' + _CSS_NAME + '=' + _CSS_STRING + r'\])'
_CSS_COMPOUND = r'(?:' + _CSS_IDENT + _CSS_PART + '*|' + _CSS_PART + '+)'
_CSS_TWIN = re.compile(_CSS_COMPOUND + '(?: ' + _CSS_COMPOUND + ')?')


# --- the plan: which keys to fix ---------------------------------------------------------

@dataclass
class Fix:
    """One key whose healed selector should replace its old code selectors."""
    key: str
    new: str                    # the healed selector (XPath)
    old: list                   # the key's active selectors from code, as stored
    confirmed: bool             # a canary success for this selector is in the journal
    role: str | None = None
    css: str | None = None      # a CSS selector for the same element (see mendwright.css)

    def for_old(self, selector):
        """What replaces one old selector: the CSS form for a CSS selector when there is
        one, else the healed XPath."""
        if self.css and classify(selector) == CSS:
            return self.css
        return self.new

    def written(self):
        """The selectors this fix writes, without repeats."""
        return list(dict.fromkeys(self.for_old(s) for s in self.old))


@dataclass
class Plan:
    fixes: list = field(default_factory=list)
    skipped: list = field(default_factory=list)       # (key, reason): keys left alone
    new_for: dict = field(default_factory=dict)       # old selector -> new, when unambiguous
    keys_for: dict = field(default_factory=dict)      # old selector -> keys that use it
    ambiguous: dict = field(default_factory=dict)     # old selector -> keys, when ambiguous
    skip_codes: dict = field(default_factory=dict)    # key -> the code of its skip (CODES)

    def skip(self, key, code, reason):
        self.skipped.append((key, reason))
        self.skip_codes[key] = code


def _text(value):
    return value if isinstance(value, str) else ''


def _canary_state(events):
    """(key, selector) -> whether the latest heal.canary event for it was a success."""
    state = {}
    for event in events or ():
        if event.get('event') != 'heal.canary':
            continue
        key, xpath = event.get('key'), event.get('xpath')
        if isinstance(key, str) and isinstance(xpath, str):
            state[(key, xpath)] = event.get('success') is True
    return state


def _active(entry):
    out = []
    for c in entry.get('candidates') or ():
        if (isinstance(c, dict) and isinstance(c.get('xpath'), str) and c['xpath']
                and (c.get('status') or 'active') == 'active'):
            out.append(c)
    return out


def _latest(candidates):
    """The one of candidates that succeeded most recently."""
    return max(candidates, key=lambda c: _text(c.get('lastSucceededAt')))


def _written(candidate, old):
    """What a heal would be written as in place of the selectors old: its CSS form when it
    has one and they are all CSS, else its XPath (see Fix.for_old)."""
    css = candidate.get('css')
    if isinstance(css, str) and css and all(classify(s) == CSS for s in old):
        return css
    return candidate['xpath']


def _quarantined_heal(entry):
    """The newest heal of entry when it was quarantined and no active selector of the key
    has matched since, else None. The newest heals are the rows with the largest addedAt
    (a row without one counts as oldest); all of them must be quarantined."""
    heals = [c for c in entry.get('candidates') or ()
             if isinstance(c, dict) and c.get('source') == 'heal' and isinstance(c.get('xpath'), str)]
    if not any(c.get('status') == 'quarantined' for c in heals):
        return None
    newest = max(_text(c.get('addedAt')) for c in heals)
    latest = [c for c in heals if _text(c.get('addedAt')) == newest]
    if not all(c.get('status') == 'quarantined' for c in latest):
        return None
    for c in _active(entry):
        last = _text(c.get('lastSucceededAt'))
        if last and last >= newest:
            return None
    return latest[0]


def _could_be_saved(new):
    """True when new is an XPath that lxml compiles, the only kind of heal client.apply()
    saves."""
    try:
        return classify(new) == XPATH and check_syntax(new) == XPATH
    except (TypeError, ValueError):
        return False


def plan_fixes(store, events, *, include_unconfirmed=False, destructive_keys=()):
    """The Plan for a store (schema-2 dict) and its journal events (oldest first, or an
    empty list). The key is fixed when its best active selector, the one that succeeded most
    recently, came from a heal. destructive_keys are treated as marked destructive, as
    mendwright.open(destructive_keys=...) does, which the store does not save."""
    selectors = store.get('selectors') if isinstance(store, dict) else None
    selectors = selectors if isinstance(selectors, dict) else {}
    canary = _canary_state(events)
    destructive_keys = frozenset(k for k in destructive_keys or () if isinstance(k, str))
    plan = Plan()
    claims = {}                 # a selector -> the keys that use it from code or a seed
    active = {}
    for key, entry in selectors.items():
        if not isinstance(entry, dict):
            continue
        active[key] = _active(entry)
        for c in active[key]:
            if c.get('source') in ('code', 'seed'):
                claims.setdefault(c['xpath'], set()).add(key)

    for key in sorted(active):
        entry, candidates = selectors[key], active[key]
        # The one that succeeded most recently. Timestamps have a one second resolution, so
        # a heal and the code selector it replaced can tie: the heal wins the tie, because
        # a heal was only made after the code selector stopped matching. A heal that never
        # matched anything is not a fix.
        best = sorted(candidates, key=lambda c: (_text(c.get('lastSucceededAt')),
                                                 c.get('source') == 'heal'), reverse=True)[0] \
            if candidates else None
        if best is None or best.get('source') != 'heal' or not _text(best.get('lastSucceededAt')):
            # No fix, but say why a key whose newest heal was quarantined dropped out.
            gone = _quarantined_heal(entry)
            if gone is not None:
                plan.skip(key, 'quarantined', 'its newest heal ' + gone['xpath'] + ' was '
                          'quarantined (an action on it failed, a check said it was the wrong '
                          'element, or the key is marked destructive), and no selector has '
                          'worked for the key since, so there is nothing to write back.')
            continue
        new = best['xpath']
        if entry.get('destructive') is True or key in destructive_keys:
            plan.skip(key, 'destructive', 'it is marked destructive, so its heals are never '
                      'written into code. Fix the selector by hand.')
            continue
        if best.get('frameXPath'):
            plan.skip(key, 'iframe', 'its healed selector runs inside the iframe '
                      + str(best['frameXPath']) + ', and a selector in your code runs in the page '
                      'itself. Change the selector and the frame handling by hand.')
            continue
        # client.apply() saves only an XPath that lxml compiles, and css.css_selectors() only
        # the forms of _CSS_TWIN: anything else in the store was written by something else.
        if not _could_be_saved(new):
            plan.skip(key, 'not_a_selector', 'its healed selector ' + repr(new) + ' is not an '
                      'XPath that lxml can compile, and mendwright saves only heals that are, so '
                      'something else wrote it into overrides.json. Check the store; nothing is '
                      'written for this key.')
            continue
        css = best.get('css') if isinstance(best.get('css'), str) and best.get('css') else None
        if css is not None and not _CSS_TWIN.fullmatch(css):
            plan.skip(key, 'not_a_selector', 'the CSS form of its heal, ' + repr(css) + ', is not '
                      'one mendwright builds (a tag name, #id, .class and [attribute="value"], '
                      'with at most one ancestor before it), so something else wrote it into '
                      'overrides.json. Check the store; nothing is written for this key.')
            continue
        old = list(dict.fromkeys(c['xpath'] for c in candidates
                                 if c.get('source') == 'code' and c['xpath'] != new))
        if not old:
            plan.skip(key, 'seed_only', 'no selector from your code is saved for it (its '
                      'selectors come from a seed file, or locator() was called without one), so '
                      'there is no string in your code to replace. Update the seed file by hand, '
                      'or pass the healed selector to heal.locator().')
            continue
        lists = [c for c in candidates if c.get('source') == 'heal' and c.get('list') is True]
        ones = [c for c in candidates if c.get('source') == 'heal' and c.get('list') is not True]
        if lists and ones:
            # heal.locator_all(key, ...) and a heal.page shortcut or heal.driver.find_element()
            # share the key, and each healed: the newer one is no fix for the other's call.
            plan.skip(key, 'list_and_element', 'it has a heal of the list (heal.locator_all()) '
                      'and a heal of one element under the same key, and one selector in your '
                      'code cannot be both, so mendwright cannot tell which one goes where. '
                      'Change the selectors by hand, or give the list a key of its own: the list '
                      'to ' + _written(_latest(lists), old) + ', the element to '
                      + _written(_latest(ones), old))
            continue
        if best.get('list') is True:
            # Only a build of 0.8.0 before its release saved list heals, and one can be another
            # list that holds the items of an emptied one (see Healer.locator_all).
            plan.skip(key, 'list_heal', 'its heal is a heal of a list (heal.locator_all()), which '
                      "mendwright no longer makes or uses: it can't tell a list that was emptied "
                      'from one whose selector broke, so the heal can be another list. If the '
                      'selector broke, change it by hand.')
            continue
        if entry.get('listFingerprint') is not None:
            # heal.locator_all() looks the key up as a list, and the heal is of one element
            # (heal.locator(), a heal.page shortcut or heal.driver.find_element() under the same
            # key): written into the list call, it would make the list that one element.
            plan.skip(key, 'list_key', 'heal.locator_all() looks it up as a list, and its heal is '
                      'of one element, so written into the list call it would make the list match '
                      'that one element. Give the list a key of its own, or change the selector by '
                      'hand to: ' + new)
            continue
        bare = [s for s in old if _BARE_WORD.match(s)]
        if bare:
            plan.skip(key, 'bare_word', 'its old selector ' + repr(bare[0]) + ' is a bare word, '
                      'and other strings in your code can be the same word (a role= value, a dict '
                      'entry), so mendwright cannot tell which ones are the selector. Change it by '
                      'hand to: ' + new)
            continue
        by = next((b for prefix, b in _DRIVER_KEYS if key.startswith(prefix)), None)
        if (by and classify(new) == XPATH and all(classify(s) == CSS for s in old)
                and not (css and by == 'By.CSS_SELECTOR')):
            plan.skip(key, 'driver_by', 'it comes from driver.find_element(' + by + ', ...) '
                      'through heal.driver, and ' + by + ' cannot run the healed XPath. Change the '
                      'call by hand to: find_element(By.XPATH, ' + repr(new) + ')'
                      + (', or find_element(By.CSS_SELECTOR, ' + repr(css) + ')' if css else ''))
            continue
        confirmed = canary.get((key, new)) is True
        if not confirmed and not include_unconfirmed:
            plan.skip(key, 'unconfirmed', 'its heal was never confirmed (journal.jsonl has no '
                      'canary success for it; the pytest plugin or an expect= or confirm(True) '
                      'check writes one in the run that makes the heal). Check that the selector '
                      'finds the right element, then add --include-unconfirmed to write it anyway.')
            continue
        role = entry.get('expectedRole')
        plan.fixes.append(Fix(key, new, old, confirmed, role if isinstance(role, str) else None,
                              css))

    by_key = {fix.key: fix for fix in plan.fixes}
    for fix in plan.fixes:
        for selector in fix.old:
            others = claims.get(selector, set()) - {fix.key}
            blockers = [k for k in others
                        if not (k in by_key and selector in by_key[k].old
                                and by_key[k].for_old(selector) == fix.for_old(selector))]
            plan.keys_for[selector] = sorted(others | {fix.key})
            if blockers:
                plan.ambiguous[selector] = plan.keys_for[selector]
                plan.new_for.pop(selector, None)
            elif selector not in plan.ambiguous:
                plan.new_for[selector] = fix.for_old(selector)
    return plan


# --- finding files -----------------------------------------------------------------------

def _is_source_name(path, rel, globs):
    name = os.path.basename(path)
    for pattern in globs:
        target = rel.replace(os.sep, '/') if '/' in pattern else name
        if fnmatch.fnmatch(target, pattern):
            return True
    return False


def _is_link(path):
    """True for a symbolic link, or a Windows directory junction, which os.path.islink()
    does not count, so a junction cannot lead the walk outside the folders given."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, 'isjunction', None)      # Python 3.12 and newer
    if isjunction is not None:
        return isjunction(path)
    try:
        tag = getattr(os.lstat(path), 'st_reparse_tag', 0)
    except OSError:
        return False
    return bool(tag) and tag == getattr(stat, 'IO_REPARSE_TAG_MOUNT_POINT', 0xA0000003)


def _parts(path):
    return [p for p in path.replace('\\', '/').split('/') if p not in ('', '.')]


def _in_github(root):
    """True when the path root is inside a .github folder: as written, or once the links on
    the way are followed (a link or junction to .github, a short name on Windows). Folder
    names are compared without case, as Windows and macOS compare them. Only the part of the
    real path below the current folder's own real path counts, so a project checked out in a
    folder named .github is still scanned."""
    if any(p.lower() == NEVER_SCANNED for p in _parts(os.path.normpath(root))):
        return True
    real = _parts(os.path.realpath(root))
    here = _parts(os.path.realpath(os.getcwd()))
    same = 0
    while (same < min(len(real), len(here))
           and os.path.normcase(real[same]) == os.path.normcase(here[same])):
        same += 1
    return any(p.lower() == NEVER_SCANNED for p in real[same:])


def collect_files(paths, extra_globs=()):
    """(files, errors): the source files under ``paths`` (folders are walked, a file path is
    taken as it is), in a stable order. Folders in SKIP_DIRS, symbolic links and Windows
    junctions are skipped, and a path inside a .github folder is skipped even when named,
    also through a link or in another case."""
    globs = list(DEFAULT_GLOBS) + [g for g in extra_globs if g not in DEFAULT_GLOBS]
    files, errors, seen = [], [], set()

    def add(path):
        real = os.path.normcase(os.path.abspath(path))
        if real not in seen:
            seen.add(real)
            files.append(os.path.abspath(path))

    for root in paths:
        if _in_github(root):
            if not os.path.exists(root):
                errors.append('no such file or folder: ' + root + '. Check the --path value.')
            continue
        if os.path.isfile(root):
            add(root)
        elif os.path.isdir(root):
            for folder, dirs, names in os.walk(root):
                dirs[:] = sorted(d for d in dirs
                                 if d not in SKIP_DIRS and d.lower() != NEVER_SCANNED
                                 and not d.endswith('.egg-info')
                                 and not _is_link(os.path.join(folder, d)))
                for name in sorted(names):
                    path = os.path.join(folder, name)
                    rel = os.path.relpath(path, root)
                    if not _is_link(path) and _is_source_name(path, rel, globs):
                        add(path)
        else:
            errors.append('no such file or folder: ' + root + '. Check the --path value.')
    return files, errors


# --- reading a source file ---------------------------------------------------------------

def _split_lines(text):
    """The lines of text with their line endings, split only on \\r\\n, \\n and \\r (not on
    form feeds or Unicode separators, which Python source does not treat as line ends)."""
    return _LINE.findall(text)


def _line_starts(lines):
    starts, total = [], 0
    for line in lines:
        starts.append(total)
        total += len(line)
    starts.append(total)
    return starts


@dataclass
class Literal:
    line: int           # 1-based
    start: int          # offsets into the text
    end: int
    source: str         # the literal as written, quotes and prefix included
    value: str          # what it evaluates to
    col: int = 0        # 0-based character column of its first character


_STRING_START = tuple(getattr(tokenize, n) for n in ('FSTRING_START', 'TSTRING_START')
                      if hasattr(tokenize, n))
_STRING_END = tuple(getattr(tokenize, n) for n in ('FSTRING_END', 'TSTRING_END')
                    if hasattr(tokenize, n))
_STRINGISH = (tokenize.STRING,) + _STRING_START + _STRING_END


def python_literals(text):
    """The plain string literals of Python source: not bytes, not an f-string (or a string
    inside one), not a part of an implicit concatenation such as 'a' 'b'. Raises SyntaxError
    when the source cannot be tokenized."""
    lines = _split_lines(text)
    starts = _line_starts(lines)
    feed = iter(lines)
    try:
        tokens = [t for t in tokenize.generate_tokens(lambda: next(feed, ''))
                  if t.type not in (tokenize.NL, tokenize.COMMENT)]
    except (tokenize.TokenError, ValueError) as exc:
        raise SyntaxError(str(exc)) from None
    out, depth = [], 0
    for i, tok in enumerate(tokens):
        if tok.type in _STRING_START:
            depth += 1
        elif tok.type in _STRING_END:
            depth -= 1
        elif tok.type == tokenize.STRING and depth == 0:
            before = tokens[i - 1].type if i else None
            after = tokens[i + 1].type if i + 1 < len(tokens) else None
            if before in _STRINGISH or after in _STRINGISH:
                continue
            prefix = re.match(r'[A-Za-z]*', tok.string).group().lower()
            if set(prefix) & set('bft'):
                continue
            try:
                value = ast.literal_eval(tok.string)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, str):
                out.append(Literal(tok.start[0], starts[tok.start[0] - 1] + tok.start[1],
                                   starts[tok.end[0] - 1] + tok.end[1], tok.string, value,
                                   tok.start[1]))
    return out


def _decode_body(body, escaping):
    """The value of a quoted string's body in a C-like language (only \\\\ \\' \\" and \\` are
    decoded), or the body itself when the language's escapes are unknown."""
    if not escaping or '\\' not in body:
        return body
    return re.sub(r'\\([\\\'"`])', r'\1', body)


def generic_literals(text, ext):
    """Quoted strings ('...', "..." and, in C-like languages, `...` without ${}) that sit
    on one line, found line by line so a stray apostrophe cannot upset the rest of the
    file. Comments are skipped in C-like languages. Triple-quoted and multi-line strings
    are ignored."""
    c_like = ext in C_LIKE_EXTS
    escaping = ext in ESCAPING_EXTS
    lines = _split_lines(text)
    starts = _line_starts(lines)
    out, block, triple = [], False, None
    for number, raw in enumerate(lines, 1):
        line = raw.rstrip('\r\n')
        i, n = 0, len(line)
        while i < n:
            if block:
                j = line.find('*/', i)
                if j < 0:
                    break
                block, i = False, j + 2
                continue
            if triple:
                j = line.find(triple, i)
                if j < 0:
                    break
                triple, i = None, j + 3
                continue
            ch = line[i]
            if c_like and line.startswith('//', i):
                break
            if c_like and line.startswith('/*', i):
                block, i = True, i + 2
                continue
            if ch in '\'"' and line.startswith(ch * 3, i):
                triple, i = ch * 3, i + 3
                continue
            if ch in '\'"' or (ch == '`' and c_like):
                j = i + 1
                while j < n and line[j] != ch:
                    j += 2 if line[j] == '\\' else 1
                if j >= n:
                    i += 1          # a stray quote, not the start of a string
                    continue
                body = line[i + 1:j]
                if not (ch == '`' and '${' in body):
                    out.append(Literal(number, starts[number - 1] + i, starts[number - 1] + j + 1,
                                       line[i:j + 1], _decode_body(body, escaping), i))
                i = j + 1
                continue
            i += 1
    return out


# --- where a literal sits ----------------------------------------------------------------

# Keyword arguments whose value is never a selector: heal.locator()'s role=, text= and
# intent=, interrupt()'s action=, Selenium's by=, get_attribute()'s name=.
NOT_SELECTOR_KEYWORDS = frozenset({'role', 'text', 'intent', 'key', 'action', 'by', 'name'})
# Calls whose first argument is a key, not a selector, when a selector comes after it.
KEYED_CALLS = frozenset({'locator', 'locator_all', 'interrupt'})
# Selenium's By names and the strings they stand for.
_BY_NAMES = {'XPATH': 'xpath', 'CSS_SELECTOR': 'css selector', 'ID': 'id', 'NAME': 'name',
             'CLASS_NAME': 'class name', 'TAG_NAME': 'tag name', 'LINK_TEXT': 'link text',
             'PARTIAL_LINK_TEXT': 'partial link text'}
_BY_VALUES = {v: k for k, v in _BY_NAMES.items()}
SKIP = 'skip'           # not a selector at all
CSS_ONLY = 'css'        # a selector, but in a place that cannot run an XPath
FLOW = 'flow'           # an interruption's selector or a step's landmark: never rewritten
# Calls whose second argument (or this keyword) is a selector mendwright fix never rewrites:
# another key's heal written there would make the interruption act on that key's element,
# or the step wait for it.
FLOW_CALLS = {'interrupt': ('selector', 'heal.interrupt()'), 'step': ('when', 'heal.step(when=)')}
# The end of the code before a literal, in other languages, that takes a CSS selector only:
# By.cssSelector(, By.CSS_SELECTOR, css: , querySelector(, By.id(, ...
_CSS_CALL = re.compile(r'(?i)(?:css(?:[ _]?selector)?|queryselector(?:all)?|'
                       r'by\.(?:id|name|class_?name|tag_?name|link_?text))$')


def _by_of(node):
    """'By.CSS_SELECTOR' (and so on) when node is a Selenium By value, else None."""
    if isinstance(node, ast.Attribute) and node.attr in _BY_NAMES:
        return 'By.' + node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in _BY_VALUES:
        return 'By.' + _BY_VALUES[node.value]
    return None


def _func_name(call):
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ''


_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
# Methods of a dict or list that give back what it holds, and builtins that pass a list on.
_READS = frozenset({'get', 'items', 'values', 'copy', 'pop', 'setdefault'})
_PASS_ON = frozenset({'list', 'tuple', 'set', 'dict', 'sorted', 'reversed', 'enumerate', 'iter'})


def _target_names(target):
    if isinstance(target, ast.Name):
        return [target]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for e in target.elts for n in _target_names(e)]
    return []


class _FlowTrace:
    """The string literals of one file that a name, an attribute or a subscript passed to
    heal.interrupt() or heal.step(when=) can stand for: what is assigned to the name in the
    scope it belongs to, or looped over into it, a parameter's default and the arguments of
    the calls to its function in this file, what is assigned to an attribute of that name
    anywhere in the file (a class constant, self.x = ...), and the values of a dict or the
    items of a list it reads. It counts a literal that might reach the call as reaching it,
    and follows nothing out of the file: an imported constant, functools.partial and a
    string built at run time are not seen."""

    def __init__(self, tree):
        self.tree = tree
        self.scope = {}         # node -> the Module, function, lambda or class it runs in
        self.bound = {}         # scope -> names bound there
        self.globals = {}       # scope -> names declared global there
        self.sources = {}       # (scope, name) -> expressions assigned or looped into it
        self.attrs = {}         # attribute name -> expressions assigned to it
        self.calls = {}         # function name -> the calls to it
        stack = [(tree, tree)]
        while stack:            # no recursion: a long expression must not overflow the stack
            node, scope = stack.pop()
            self.scope[node] = scope
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                outer = node.decorator_list + [node.args] + ([node.returns] if node.returns else [])
                stack += [(c, scope) for c in outer] + [(s, node) for s in node.body]
                self._bind(scope, node.name)
                self._params(node)
            elif isinstance(node, ast.Lambda):
                stack += [(node.args, scope), (node.body, node)]
                self._params(node)
            elif isinstance(node, ast.ClassDef):
                outer = node.decorator_list + node.bases + [k.value for k in node.keywords]
                stack += [(c, scope) for c in outer] + [(s, node) for s in node.body]
                self._bind(scope, node.name)
            else:
                stack += [(c, scope) for c in ast.iter_child_nodes(node)]
        for node in list(self.scope):
            self._record(node)

    def _bind(self, scope, name, source=None):
        self.bound.setdefault(scope, set()).add(name)
        if source is not None:
            self.sources.setdefault((scope, name), []).append(source)

    def _params(self, func):
        args = func.args
        positional = args.posonlyargs + args.args
        defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
        pairs = (list(zip(positional, defaults, strict=True))
                 + list(zip(args.kwonlyargs, args.kw_defaults, strict=True)))
        for arg, default in pairs:
            self._bind(func, arg.arg, default)
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                self._bind(func, arg.arg)

    def _assign(self, scope, target, value):
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) \
                and len(target.elts) == len(value.elts) \
                and not any(isinstance(e, ast.Starred) for e in target.elts + value.elts):
            for t, v in zip(target.elts, value.elts, strict=True):
                self._assign(scope, t, v)
            return
        if isinstance(target, ast.Attribute):
            self.attrs.setdefault(target.attr, []).append(value)
        for name in _target_names(target):
            self._bind(scope, name.id, value)
            if isinstance(scope, ast.ClassDef):       # a class constant, read as Class.NAME
                self.attrs.setdefault(name.id, []).append(value)

    def _record(self, node):
        scope = self.scope[node]
        if isinstance(node, ast.Assign):
            for target in node.targets:
                self._assign(scope, target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value is not None:
            self._assign(scope, node.target, node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            for name in _target_names(node.target):
                self._bind(scope, name.id, node.iter)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            for name in _target_names(node.optional_vars):
                self._bind(scope, name.id, node.context_expr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                self._bind(scope, (alias.asname or alias.name).split('.')[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            self._bind(scope, node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            self.globals.setdefault(scope, set()).update(node.names)
        elif isinstance(node, ast.Call):
            name = _func_name(node)
            if name:
                self.calls.setdefault(name, []).append(node)

    def _binding_scope(self, name, scope):
        if name in self.globals.get(scope, ()):
            return self.tree
        through_function = False
        while True:
            if name in self.bound.get(scope, ()) and not (
                    isinstance(scope, ast.ClassDef) and through_function):
                return scope
            if scope is self.tree:
                return None
            through_function = through_function or isinstance(scope, _FUNCS)
            scope = self.scope[scope]

    def _call_arguments(self, func, name):
        """The arguments the calls in this file pass to parameter name of func."""
        positional = [a.arg for a in func.args.posonlyargs + func.args.args]
        method = isinstance(self.scope[func], ast.ClassDef)
        out = []
        for call in self.calls.get(getattr(func, 'name', None), ()):
            if name in positional:
                index = positional.index(name) - (1 if method and isinstance(call.func, ast.Attribute)
                                                  else 0)
                if 0 <= index < len(call.args) and not any(
                        isinstance(a, ast.Starred) for a in call.args[:index + 1]):
                    out.append(call.args[index])
            out += [k.value for k in call.keywords if k.arg == name]
        return out

    def _is_param(self, func, name):
        args = func.args
        return name in [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]

    def literals(self, expr):
        """The str Constant nodes expr can stand for."""
        out, seen, work = [], set(), [expr]
        while work:
            node = work.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            scope = self.scope.get(node, self.tree)
            if isinstance(node, ast.Constant):
                if isinstance(node.value, str):
                    out.append(node)
            elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                work += node.elts
            elif isinstance(node, ast.Dict):
                work += node.values
            elif isinstance(node, ast.Starred):
                work.append(node.value)
            elif isinstance(node, ast.IfExp):
                work += [node.body, node.orelse]
            elif isinstance(node, ast.BoolOp):
                work += node.values
            elif isinstance(node, ast.Subscript):
                work.append(node.value)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in _READS:
                work += [node.func.value] + node.args[1:2]
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in _PASS_ON:
                work += node.args
            elif isinstance(node, ast.Name):
                where = self._binding_scope(node.id, scope)
                if where is not None:
                    work += [s for s in self.sources.get((where, node.id), ()) if s is not None]
                    if isinstance(where, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                            self._is_param(where, node.id):
                        work += self._call_arguments(where, node.id)
            elif isinstance(node, ast.Attribute):
                work += self.attrs.get(node.attr, ())
        return out


def python_contexts(text):
    """{(line, column): context} for the string literals of Python source that are not a
    plain selector. The context is SKIP for a literal that is no selector at all (a role=,
    text= or intent= value, or the key in heal.locator(key, selector)), (FLOW, what) for the
    selector of heal.interrupt(key, selector) or the landmark of heal.step(name, when), or
    one string of a list or tuple passed there, or (CSS_ONLY, by) for one that sits where an
    XPath cannot run: the value next to a Selenium By other than By.XPATH (in a call or a
    (By, value) pair), or an argument of a function with css in its name. Columns count
    characters, as tokenize does. {} when the source does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    lines = _split_lines(text)
    out = {}
    through = []        # (expression, what): a selector passed to a FLOW call by name

    def mark(node, context):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            return
        line = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ''
        col = len(line.encode('utf-8')[:node.col_offset].decode('utf-8', 'replace'))
        out.setdefault((node.lineno, col), context)

    def by_pair(by_node, value_node):
        by = _by_of(by_node)
        if by is not None and by != 'By.XPATH':
            mark(value_node, (CSS_ONLY, by))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _func_name(node)
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if name in FLOW_CALLS:
                keyword, what = FLOW_CALLS[name]
                values = node.args[1:2] + ([keywords[keyword]] if keyword in keywords else [])
                for value in values:
                    items = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
                    for item in items:
                        if isinstance(item, ast.Constant):
                            mark(item, (FLOW, what))
                        else:
                            through.append((item, what))
            for arg, value in keywords.items():
                if arg in NOT_SELECTOR_KEYWORDS:
                    mark(value, SKIP)
            args = node.args
            keyed = name in KEYED_CALLS and (len(args) >= 2 or 'selector' in keywords
                                             or 'inline_xpath' in keywords)
            if keyed and args:
                mark(args[0], SKIP)
            elif len(args) >= 2:
                by_pair(args[0], args[1])
            if 'by' in keywords:
                value = keywords.get('value', args[0] if args else None)
                if value is not None:
                    by_pair(keywords['by'], value)
            if 'css' in name.lower():
                for arg in list(args) + [keywords[k] for k in ('value', 'selector') if k in keywords]:
                    mark(arg, (CSS_ONLY, name + '()'))
        elif isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == 2:
            by_pair(node.elts[0], node.elts[1])
    if through:
        trace = _FlowTrace(tree)
        for expr, what in through:
            via = ast.get_source_segment(text, expr) or ''
            via = ' '.join(via.split())
            via = via if len(via) <= 60 else via[:57] + '...'
            for literal in trace.literals(expr):
                mark(literal, (FLOW, what, via))
    return out


def generic_context(line, col):
    """(CSS_ONLY, what) when the code before column ``col`` of ``line`` takes a CSS selector
    only (By.cssSelector(, querySelector(, css: ...), else None."""
    before = re.sub(r'[\s(,:=>\'"`]+$', '', line[:col])
    match = _CSS_CALL.search(before)
    return (CSS_ONLY, match.group(0)) if match else None


# --- writing a new literal ---------------------------------------------------------------

def _py_escape(ch):
    if ch == '\\':
        return '\\\\'
    if ch == '\n':
        return '\\n'
    if ch == '\r':
        return '\\r'
    if ch == '\t':
        return '\\t'
    if ch == ' ' or ch.isprintable():
        return ch
    code = ord(ch)
    if code < 0x100:
        return '\\x%02x' % code
    return ('\\u%04x' % code) if code < 0x10000 else ('\\U%08x' % code)


def _clean_body(value, quote, raw):
    """The text between ``quote`` marks for value, when it needs no backslash before a
    quote character; None when it does."""
    qchar = quote[0]
    if len(quote) == 3:
        if qchar * 3 in value or value.endswith(qchar):
            return None
    elif qchar in value:
        return None
    if raw:
        if '\n' in value or '\r' in value or (value.endswith('\\') and
                                              (len(value) - len(value.rstrip('\\'))) % 2):
            return None
        return value
    return ''.join(_py_escape(ch) for ch in value)


def python_literal(new, token):
    """A Python literal for the string ``new`` written like ``token``: its prefix and quote
    style when they can hold it without escaping, else the other quote style, else the
    original quotes with the quote character escaped. ast.literal_eval(result) == new."""
    match = re.match(r'([A-Za-z]*)(\'\'\'|"""|\'|")', token)
    prefix, quote = match.group(1), match.group(2)
    qchar = quote[0]
    other = '"' if qchar == "'" else "'"
    quotes = [quote, other * len(quote)]
    was_raw = 'r' in prefix.lower()
    plain = ''.join(c for c in prefix if c not in 'rR')
    for q in quotes:
        for raw in ((True, False) if was_raw else (False,)):
            body = _clean_body(new, q, raw)
            if body is not None:
                literal = (prefix if raw else plain) + q + body + q
                if _evaluates_to(literal, new):
                    return literal
    body = ''.join(_py_escape(ch) for ch in new).replace(qchar, '\\' + qchar)
    literal = plain + quote + body + quote
    return literal if _evaluates_to(literal, new) else repr(new)


def _evaluates_to(literal, value):
    try:
        return ast.literal_eval(literal) == value
    except (ValueError, SyntaxError):
        return False


def generic_literal(new, token, ext):
    """A quoted string for ``new`` written like ``token`` (a literal found by
    generic_literals), or None when it cannot be written without guessing: a newline in the
    selector, or a language whose escapes are unknown and a selector that needs one."""
    escaping = ext in ESCAPING_EXTS
    qchar = token[0]
    if '\n' in new or '\r' in new:
        return None
    if ext in DOUBLE_QUOTE_ONLY_EXTS:
        order = [qchar]
    else:
        order = [qchar] + [q for q in ('\'', '"') if q != qchar]
    def needs_escape(q):
        return q in new or (q == '`' and '${' in new)

    for q in order:
        if not needs_escape(q) and (escaping or '\\' not in new):
            body = new.replace('\\', '\\\\') if escaping else new
            return _checked_generic(q + body + q, new, escaping)
    if not escaping:
        return None
    body = new.replace('\\', '\\\\').replace(qchar, '\\' + qchar)
    return _checked_generic(qchar + body + qchar, new, escaping)


def _checked_generic(literal, new, escaping):
    return literal if _decode_body(literal[1:-1], escaping) == new else None


# --- checking a rewritten Python file ----------------------------------------------------

def _shape(tree):
    """(the string constants in order, the tree with every string blanked)."""
    values = []

    class Blank(ast.NodeTransformer):
        def visit_Constant(self, node):
            if isinstance(node.value, str):
                values.append(node.value)
                return ast.copy_location(ast.Constant(value=''), node)
            return node

    return values, ast.dump(Blank().visit(tree))


def _python_check(before, after, replaced):
    """None when ``after`` is ``before`` with exactly the strings in ``replaced`` (a list of
    (old value, new value)) changed; else the reason it is not."""
    try:
        old_values, old_shape = _shape(ast.parse(before))
    except (SyntaxError, ValueError) as exc:
        return 'the file does not parse before the change (' + str(exc) + ')'
    try:
        new_values, new_shape = _shape(ast.parse(after))
    except (SyntaxError, ValueError) as exc:
        return 'the file would no longer parse (' + str(exc) + ')'
    if old_shape != new_shape or len(old_values) != len(new_values):
        return 'the change would alter more than the selector strings'
    changed = sorted((o, n) for o, n in zip(old_values, new_values, strict=True) if o != n)
    if changed != sorted(replaced):
        return 'the strings that changed are not the ones that were planned'
    return None


# --- scanning and rewriting --------------------------------------------------------------

@dataclass
class Replacement:
    path: str           # as shown to the user
    line: int
    keys: list
    selector: str       # the stored old selector
    was: str            # the literal as written
    now: str            # the literal as it will be written


@dataclass
class FileChange:
    path: str           # absolute
    shown: str
    encoding: str
    original: bytes
    updated: bytes
    replacements: list
    diff: list


@dataclass
class Blocked:
    shown: str
    items: list         # (line, selector, keys)
    held_back: int      # fixable replacements in the same file that were not made


@dataclass
class FixResult:
    plan: Plan
    scanned: int = 0
    changes: list = field(default_factory=list)
    blocked: list = field(default_factory=list)
    unmatched: list = field(default_factory=list)       # Fix objects with no literal anywhere
    holding: dict = field(default_factory=dict)          # healed selector -> where code has it
    notes: list = field(default_factory=list)            # left alone, with the reason
    # One {'code', 'path', 'line', 'text'} per note, in the same order (see left_alone).
    note_records: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    written: list = field(default_factory=list)
    has_robot: bool = False

    def note(self, code, path, line, text):
        self.notes.append(text)
        self.note_records.append({'code': code, 'path': path, 'line': line, 'text': text})


def shown_path(path):
    """path relative to the current folder when it is under it (with '/'), else absolute."""
    try:
        rel = os.path.relpath(path)
    except ValueError:
        return path
    return path if rel.startswith('..') else rel.replace(os.sep, '/')


def _needles(selectors):
    """Fragments of the selectors that appear verbatim in a file even when quotes and
    backslashes are escaped there: used to skip files that cannot match without parsing."""
    out = set()
    for selector in selectors:
        pieces = [p for p in _NEEDLE_SPLIT.split(selector) if p]
        out.add(max(pieces, key=len) if pieces else '')
    return out


def _lookup(value, table):
    """The key of ``table`` that string ``value`` stands for (as written or normalized)."""
    text = value.strip()
    if not text:
        return None
    if value in table:
        return value
    try:
        spelled = normalize(value)
    except (TypeError, ValueError):
        return None
    return spelled if spelled in table else None


def _read_bytes(path):
    """The file's bytes, or None when it is unreadable, too big or looks binary."""
    try:
        with open(path, 'rb') as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_FILE_BYTES or b'\0' in data[:8192]:
        return None
    return data


def _decode_source(path, data):
    """(text, encoding) for source bytes, or None when they are not text mendwright can
    write back unchanged. Python files honour a BOM or a coding comment."""
    if path.lower().endswith(PYTHON_EXTS):
        try:
            encoding = tokenize.detect_encoding(io.BytesIO(data).readline)[0]
            return data.decode(encoding), encoding
        except (SyntaxError, UnicodeDecodeError, LookupError):
            return None
    encoding = 'utf-8-sig' if data.startswith(codecs.BOM_UTF8) else 'utf-8'
    try:
        return data.decode(encoding), encoding
    except UnicodeDecodeError:
        return None


def _diff(shown, before, after):
    lines = difflib.unified_diff(_split_lines(before), _split_lines(after),
                                 'a/' + shown, 'b/' + shown, n=3, lineterm='')
    return [line.rstrip('\r\n') for line in lines]


def _flow_note(where, value, what, keys, via=''):
    """The note for a literal under heal.interrupt() or heal.step(when=) that equals the old
    selector of keys. via is the code passed there when the literal reaches the call through
    a name, an attribute or a subscript."""
    if what == 'heal.step(when=)':
        rule, thing = "never rewrites a step's landmark", "the step's landmark"
    else:
        rule, thing = 'never rewrites the selector of an interruption', 'the interruption'
    return (where + ' was not changed: ' + repr(value) + ' is passed to ' + what
            + (' through ' + via if via else '') + ', and mendwright fix ' + rule
            + '. It equals the old selector of ' + ', '.join(keys) + ', so if ' + thing
            + ' broke too, change it by hand.')


def scan_file(path, plan, result, needles):
    """Look for the plan's old selectors in one file. Adds a FileChange, a Blocked entry,
    a note or an error to ``result``, notes where a healed selector is already in the file,
    and returns the set of old selectors it found. ``needles`` come from _needles()."""
    found = set()
    data = _read_bytes(path)
    if data is None or not any(n.encode('utf-8') in data for n in needles):
        return found
    decoded = _decode_source(path, data)
    if decoded is None:
        return found
    text, encoding = decoded
    shown = shown_path(path)
    ext = os.path.splitext(path)[1].lower()
    python = ext in PYTHON_EXTS
    if ext == '.robot':
        result.has_robot = True
    try:
        if python:
            ast.parse(text)
        literals = python_literals(text) if python else generic_literals(text, ext)
    except (SyntaxError, ValueError) as exc:
        result.note('not_python', shown, None, shown + ' was not changed: it could not be read '
                    'as Python (' + str(exc) + '). Fix the syntax error and run again, or edit '
                    'the selector by hand.')
        return found

    table = {**plan.new_for, **plan.ambiguous}
    healed = {s: s for fix in plan.fixes for s in [fix.new] + fix.written()}
    contexts = python_contexts(text) if python else {}
    lines = None if python else _split_lines(text)
    hits, blocked = [], []
    for lit in literals:
        selector = _lookup(lit.value, table)
        if selector is None:
            already = _lookup(lit.value, healed)
            if already is not None:
                result.holding.setdefault(already, []).append(shown + ':' + str(lit.line))
            continue
        if python:
            context = contexts.get((lit.line, lit.col))
        else:
            context = generic_context(lines[lit.line - 1].rstrip('\r\n'), lit.col)
        if context == SKIP:
            continue        # a key, or a role= value: equal to the selector, but not one
        found.add(selector)
        if context and context[0] == FLOW:
            result.note('flow_selector', shown, lit.line, _flow_note(
                shown + ':' + str(lit.line), lit.value, context[1], plan.keys_for[selector],
                context[2] if len(context) > 2 else ''))
            continue
        if selector in plan.ambiguous:
            blocked.append((lit.line, selector, plan.ambiguous[selector]))
            continue
        new = plan.new_for[selector]
        if lit.value == new:
            continue
        if context and context[0] == CSS_ONLY and classify(new) == XPATH:
            result.note('css_only', shown, lit.line, shown + ':' + str(lit.line) + ' was not '
                        'changed: ' + lit.source + ' is passed to ' + context[1] + ', which cannot '
                        'run the healed XPath. Change the call to take an XPath (By.XPATH in '
                        'Selenium) and the selector to: ' + new)
            continue
        if python:
            now = python_literal(new, lit.source)
        elif ext == '.cs' and lit.start and text[lit.start - 1] == '@':
            now = None   # a C# verbatim string: "" is its only escape
        else:
            now = generic_literal(new, lit.source, ext)
        if now is None:
            result.note('no_escape', shown, lit.line, shown + ':' + str(lit.line) + ' was not '
                        'changed: ' + lit.source + ' is the old selector of '
                        + ', '.join(plan.keys_for[selector]) + ', but the healed selector cannot '
                        'be written into this kind of file without guessing at its escapes. '
                        'Replace it by hand with: ' + new)
            continue
        hits.append((lit, selector, now, new))
    if blocked:
        result.blocked.append(Blocked(shown, blocked, len(hits)))
        return found
    if not hits:
        return found

    updated = text
    for lit, _selector, now, _new in sorted(hits, key=lambda h: h[0].start, reverse=True):
        updated = updated[:lit.start] + now + updated[lit.end:]
    if python:
        problem = _python_check(text, updated, [(h[0].value, h[3]) for h in hits])
        if problem:
            result.errors.append(shown + ' was not changed: ' + problem + '. This is a bug in '
                                 'mendwright fix; edit the selector by hand and report it.')
            return found
    try:
        new_data = updated.encode(encoding)
    except UnicodeEncodeError:
        result.errors.append(shown + ' was not changed: the healed selector has characters '
                             'that its encoding (' + encoding + ') cannot hold. Edit it by hand.')
        return found
    replacements = [Replacement(shown, lit.line, plan.keys_for[sel], sel, lit.source, now)
                    for lit, sel, now, _new in sorted(hits, key=lambda h: h[0].start)]
    result.changes.append(FileChange(path, shown, encoding, data, new_data, replacements,
                                     _diff(shown, text, updated)))
    return found


def build_result(plan, paths=('.',), globs=()):
    """Scan the source files and work out every change. Nothing is written."""
    result = FixResult(plan)
    files, errors = collect_files(list(paths) or ['.'], globs)
    result.errors.extend(errors)
    result.scanned = len(files)
    found = set()
    if plan.fixes:
        written = {s for f in plan.fixes for s in [f.new] + f.written()}
        needles = _needles(set(plan.new_for) | set(plan.ambiguous) | written)
        for path in files:
            found |= scan_file(path, plan, result, needles)
    result.unmatched = [fix for fix in plan.fixes if not any(s in found for s in fix.old)]
    return result


# --- writing -----------------------------------------------------------------------------

def _write_atomic(change):
    """Replace the file with its new bytes through a temp file next to it. Raises OSError,
    or ValueError when the file changed since it was read."""
    with open(change.path, 'rb') as handle:
        if handle.read() != change.original:
            raise ValueError('it changed while mendwright fix was running')
    mode = stat.S_IMODE(os.stat(change.path).st_mode)
    fd, tmp = tempfile.mkstemp(prefix='.mendwright-fix-', dir=os.path.dirname(change.path))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(change.updated)
        os.chmod(tmp, mode)
        delay = 0.01
        for attempt in range(_WRITE_TRIES):
            try:
                os.replace(tmp, change.path)
                break
            except PermissionError:
                if attempt == _WRITE_TRIES - 1:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.2)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def apply_result(result):
    """Write every change in ``result``. A file that cannot be written is left as it was
    and added to result.errors; the others are still written."""
    for change in result.changes:
        try:
            _write_atomic(change)
        except (OSError, ValueError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            result.errors.append(change.shown + ' was not changed: ' + reason + '. Close the '
                                 'file in other programs, or edit it by hand, then run again.')
            continue
        result.written.append(change.shown)


# --- the text ----------------------------------------------------------------------------

def _count(n, word):
    return str(n) + ' ' + word + ('' if n == 1 else 's')


def left_alone(result):
    """What format_lines lists under "Left alone", in its order (the keys skipped, the files
    blocked, the notes, then the fixes found nowhere), as {'code', 'key', 'path', 'line',
    'text'} dicts. text is the line without its indent; code is one of CODES."""
    plan = result.plan
    out = [{'code': plan.skip_codes.get(key, 'other'), 'key': key, 'path': None, 'line': None,
            'text': key + ': ' + reason} for key, reason in plan.skipped]
    for blocked in result.blocked:
        selectors = sorted({i[1] for i in blocked.items})
        keys = sorted({k for i in blocked.items for k in i[2]})
        out.append({'code': 'ambiguous', 'key': None, 'path': blocked.shown,
                    'line': blocked.items[0][0] if blocked.items else None,
                    'text': blocked.shown + ' was not touched: ' + _quote_list(selectors)
                    + (' belongs' if len(selectors) == 1 else ' belong') + ' to more than one key ('
                    + ', '.join(keys) + '), so mendwright cannot tell which healed selector goes at '
                    + _count(len(blocked.items), 'place')
                    + (' (line ' if len(blocked.items) == 1 else ' (lines ')
                    + ', '.join(str(i[0]) for i in blocked.items) + '). '
                    + ('The ' + _count(blocked.held_back, 'other replacement') + ' in this file '
                       + ('was' if blocked.held_back == 1 else 'were') + ' held back too. '
                       if blocked.held_back else '')
                    + 'Give each key its own selector string, or edit the file by hand.'})
    records = list(result.note_records)
    if len(records) != len(result.notes):      # notes added without note()
        records = [{'code': 'other', 'path': None, 'line': None, 'text': text}
                   for text in result.notes]
    out.extend({'code': r['code'], 'key': None, 'path': r['path'], 'line': r['line'],
                'text': r['text']} for r in records)
    for fix in result.unmatched:
        where = next((result.holding[s] for s in fix.written() + [fix.new]
                      if s in result.holding), None)
        if where:
            out.append({'code': 'already_in_code', 'key': fix.key, 'path': None, 'line': None,
                        'text': fix.key + ': your code already passes the healed selector ('
                        + ', '.join(where[:3]) + (', and more' if len(where) > 3 else '')
                        + '), so there is nothing to change. Run your tests once and mendwright '
                        "records it as your code's selector."})
            continue
        out.append({'code': 'not_found', 'key': fix.key, 'path': None, 'line': None,
                    'text': fix.key + ': no string equal to ' + _quote_list(fix.old) + ' was found in '
                    + _count(result.scanned, 'file') + '. Add --path or --glob (for example --glob '
                    "'*.ts') if it is somewhere else, or change it by hand to: " + fix.written()[0]
                    + (' mendwright fix only rewrites quoted strings, so unquoted Robot Framework '
                       'arguments are not changed.' if result.has_robot else '')})
    return out


def format_lines(result, *, apply):
    """The text `mendwright fix` prints on stdout, as a list of lines."""
    lines = []
    fixed = {k for c in result.changes for r in c.replacements for k in r.keys}
    n_rep = sum(len(c.replacements) for c in result.changes)
    n_files = len(result.changes)
    if not n_rep:
        lines.append('mendwright fix: nothing to write back.')
    elif apply:
        wrote = len(result.written)
        lines.append('mendwright fix: wrote ' + _count(n_rep, 'replacement') + ' in '
                     + _count(n_files, 'file') + ' (' + _count(len(fixed), 'key') + ').'
                     if wrote == n_files else
                     'mendwright fix: wrote ' + _count(wrote, 'file') + ' of ' + str(n_files)
                     + '. The errors are listed after the changes.')
    else:
        lines.append('mendwright fix: ' + _count(n_rep, 'replacement') + ' in '
                     + _count(n_files, 'file') + ' would put ' + _count(len(fixed), 'healed selector')
                     + ' into your code. Dry run, nothing was changed.')

    for change in result.changes:
        lines.append('')
        lines.extend(change.diff)

    if fixed:
        for fix in result.plan.fixes:
            if fix.key not in fixed:
                continue
            lines.append('')
            lines.append(fix.key + ('  (' + fix.role + ')' if fix.role else '') + '  '
                         + ('healed, confirmed' if fix.confirmed
                            else 'healed, NOT confirmed'))
            for selector in fix.old:
                lines.append('  was  ' + selector)
            for selector in fix.written():
                lines.append('  now  ' + selector)
            places = ['  ' + r.path + ':' + str(r.line) for c in result.changes
                      for r in c.replacements if fix.key in r.keys]
            lines.extend(places)
            if len(fix.old) > 1:
                lines.append('  (it had ' + str(len(fix.old)) + ' selectors in code, and each '
                             'found one is replaced by the healed selector)')

    left = [item['text'] for item in left_alone(result)]
    if left:
        lines.append('')
        lines.append('Left alone:')
        lines.extend('  ' + line for line in left)

    if n_rep and not apply:
        lines.append('')
        lines.append('Run the same command with --apply to write these changes.')
    if n_rep:
        lines.append('')
        lines.append('Once your code holds the new selector, mendwright tries it first on the next '
                     'run, because selectors in your code win over saved heals.')
    elif not left:
        lines.append('No key has a healed selector ahead of the one in your code.')
    return lines


def _quote_list(items):
    return ', '.join(repr(i) for i in items)


# --- machine output: JSON and Markdown ---------------------------------------------------

# What each code in left_alone() stands for, as the Markdown lists it after the key or path.
CODES = {
    'destructive': 'is marked destructive, so its heals are never written',
    'iframe': 'was healed inside an iframe, which a selector in your code does not reach',
    'not_a_selector': 'has a healed selector mendwright could not have saved',
    'seed_only': 'has no selector from your code to replace',
    'list_and_element': 'has a list heal and a one-element heal under the same key',
    'list_heal': 'has a heal of a list, which mendwright no longer uses',
    'list_key': 'is looked up as a list, and its heal is of one element',
    'bare_word': 'has a bare word as its old selector',
    'driver_by': 'comes from a `By` that cannot run the healed XPath',
    'unconfirmed': 'its heal was never confirmed',
    'quarantined': 'its newest heal was quarantined',
    'ambiguous': 'holds a string that belongs to more than one key',
    'not_python': 'could not be read as Python',
    'css_only': 'passes the selector where only CSS runs',
    'no_escape': 'needs escapes mendwright does not know for this kind of file',
    'flow_selector': 'passes the old selector to `heal.interrupt()` or `heal.step(when=)`, which '
                     'mendwright never rewrites',
    'already_in_code': 'your code already has the healed selector',
    'not_found': 'no string equal to the old selector was found',
}
# A role shown as it is in the Markdown; any other role is shown in a code span.
_ROLE_WORD = re.compile(r'[a-z]+(?:-[a-z]+)*')
_MORE = 'Run mendwright fix with this store to see them all.'


def _slashes(path):
    return path.replace(os.sep, '/') if isinstance(path, str) else path


def _str(value):
    return value if isinstance(value, str) else None


def _latest_event(events, name, key, xpath):
    found = None
    for event in events or ():
        if (isinstance(event, dict) and event.get('event') == name and event.get('key') == key
                and event.get('xpath') == xpath):
            found = event
    return found


def _found_by(events, fix):
    """How the heal was found, from the latest heal.applied for it, or None."""
    event = _latest_event(events, 'heal.applied', fix.key, fix.new)
    if event is None:
        return None
    source, provider = event.get('source'), event.get('provider')
    if 'fingerprint' in (source, provider):
        tier = 'fingerprint'
    elif source in ('intent', 'frame'):
        tier = source
    else:
        tier = 'model'
    return {'tier': tier, 'provider': _str(provider), 'model': _str(event.get('model')),
            'at': _str(event.get('ts'))}


def _confirmed_by(events, fix):
    """What confirmed the heal, from the canary that decides it (see _canary_state), or
    None when it is not confirmed. by and test are None for a canary from before 0.9.0."""
    event = _latest_event(events, 'heal.canary', fix.key, fix.new)
    if event is None or event.get('success') is not True:
        return None
    return {'by': _str(event.get('by')), 'test': _str(event.get('test')),
            'at': _str(event.get('ts'))}


def plan_hash(replacements):
    """The SHA-256 (hex) of what the replacements write where: each one's path, old selector,
    new selector and keys, sorted. Line numbers and quoting are left out, so an unrelated edit
    that moves a line keeps the hash, and one more copy of an old selector changes it.
    ``replacements`` are the dicts of to_json()."""
    rows = sorted([r['path'], r['selector'], r['new'], sorted(r['keys'])] for r in replacements)
    text = json.dumps(rows, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(text.encode('ascii')).hexdigest()


def to_json(result, *, apply, store_path, journal_path, events):
    """The `mendwright fix --format json` document for a FixResult (schema 1). events are
    the journal's (None when there is no journal)."""
    from . import __version__
    plan = result.plan
    replacements = [{'path': _slashes(r.path), 'line': r.line, 'keys': list(r.keys),
                     'selector': r.selector, 'new': plan.new_for.get(r.selector),
                     'was': r.was, 'now': r.now}
                    for change in result.changes for r in change.replacements]
    fixed = {k for r in replacements for k in r['keys']}
    written_back = []
    for fix in plan.fixes:
        if fix.key not in fixed:
            continue
        written_back.append({
            'key': fix.key, 'role': fix.role, 'old': list(fix.old), 'now': fix.written(),
            'xpath': fix.new, 'css': fix.css, 'confirmed': fix.confirmed,
            'found_by': _found_by(events, fix), 'confirmed_by': _confirmed_by(events, fix),
            'places': [{'path': r['path'], 'line': r['line']} for r in replacements
                       if fix.key in r['keys']]})
    left = [dict(item, path=_slashes(item['path'])) for item in left_alone(result)]
    return {
        'schema': 1,
        'mendwright': __version__,
        'store': os.path.abspath(store_path),
        'journal': {'path': os.path.abspath(journal_path), 'exists': events is not None},
        'applied': bool(apply),
        'scanned': result.scanned,
        'plan': plan_hash(replacements),
        'written_back': written_back,
        'replacements': replacements,
        'left_alone': left,
        'files': [{'path': _slashes(c.shown), 'replacements': len(c.replacements),
                   'written': c.shown in result.written} for c in result.changes],
        'errors': [str(e) for e in result.errors],
    }


def _md_found(found, code):
    if not isinstance(found, dict):
        return 'unknown (no heal.applied in journal.jsonl)'
    tier = found.get('tier')
    if tier == 'fingerprint':
        return 'fingerprint (no model)'
    if tier == 'intent':
        return 'model, from intent='
    if tier == 'frame':
        return 'inside an iframe'
    named = [code(v) for v in (found.get('provider'), found.get('model'))
             if isinstance(v, str) and v]
    return 'model: ' + ', '.join(named) if named else 'model'


def _md_confirmed(confirmed, code):
    if not isinstance(confirmed, dict):
        return '**not confirmed**'
    by = confirmed.get('by')
    if by == 'test':
        test = confirmed.get('test')
        return 'passing test ' + code(test) if isinstance(test, str) and test else 'passing test'
    shown = {'expect': '`expect=` on an action', 'expect_value': '`expect_value=` on a read',
             'confirm': '`confirm(True)`'}
    if by in shown:
        return shown[by]
    if by is None:
        return 'a check before mendwright 0.9.0, which did not record which'
    return code(by)


def _md_row(item, prefix):
    def code(text):
        return _md.code(text, table=True)

    key = code(item.get('key'))
    role = item.get('role')
    if isinstance(role, str) and _ROLE_WORD.fullmatch(role):
        key += ' (' + role + ')'
    elif role:
        key += ' (' + code(role) + ')'
    places = item.get('places') or []
    where = ', '.join(code(prefix + str(p.get('path')) + ':' + str(p.get('line')))
                      for p in places[:3])
    if len(places) > 3:
        where += ' and ' + str(len(places) - 3) + ' more'
    cells = [key, ', '.join(code(s) for s in item.get('old') or []),
             ', '.join(code(s) for s in item.get('now') or []),
             _md_found(item.get('found_by'), code), _md_confirmed(item.get('confirmed_by'), code),
             where]
    return '| ' + ' | '.join(cells) + ' |'


def _md_item(item, prefix):
    """(the list line, the lines of its <details> block) for one item of left_alone. The
    block ends with a blank line, so the next item is not read as part of its HTML."""
    if item.get('key') is not None:
        subject = _md.code(item['key'])
    elif item.get('path') is not None:
        line = item.get('line')
        subject = _md.code(prefix + str(item['path']) + (':' + str(line) if line else ''))
    else:
        subject = 'mendwright fix'
    phrase = CODES.get(item.get('code'), 'was left alone')
    why = ['  <details><summary>Why</summary>', '']
    why += [('  ' + line) if line else '' for line in _md.fence(item.get('text') or '').split('\n')]
    why += ['', '  </details>', '']
    return '- ' + subject + ': ' + phrase + '.', why


def render_markdown(data, *, heading='##', limit=54000, path_prefix=''):
    """The Markdown for a to_json() document: a table of the selectors written back and a
    list of what was left alone, at most ``limit`` characters. Every value from the store,
    the journal or the source files is in a code span or a fenced block (mendwright._md).
    path_prefix goes before every path, such as the working directory under the repository
    root. Over the limit, the <details> blocks go first, then items of "Left alone", then
    rows of the table, and a list that was cut ends with a line that says how many are not
    shown."""
    sub = heading + '#'
    written = list(data.get('written_back') or [])
    left = list(data.get('left_alone') or [])
    head = [heading + ' mendwright fix', '']
    if written:
        title = ('Selectors written back (' + str(len(written)) + ')' if data.get('applied')
                 else 'Selectors to write back (' + str(len(written)) + ', dry run)')
        head += [sub + ' ' + title, '', '| Key | Was | Now | Found by | Confirmed by | Where |',
                 '| --- | --- | --- | --- | --- | --- |']
    else:
        head += ['Nothing to write back.']
    rows = [_md_row(item, path_prefix) for item in written]
    items = [_md_item(item, path_prefix) for item in left]

    def more(n):
        return '…and ' + str(n) + ' more. ' + _MORE

    def build(n_rows, n_items, details):
        lines = head + rows[:n_rows]
        if n_rows < len(rows):
            lines += ['', more(len(rows) - n_rows)]
        if items:
            lines += ['', sub + ' Left alone (' + str(len(items)) + ')', '']
            for short, why in items[:n_items]:
                lines.append(short)
                if details:
                    lines += why
            if n_items < len(items):
                if lines[-1]:
                    lines.append('')
                lines.append(more(len(items) - n_items))
        while lines and not lines[-1]:
            lines.pop()
        return '\n'.join(lines) + '\n'

    def most(fits, top):
        """The largest n in 0..top for which fits(n), or -1 when none does."""
        if not fits(0):
            return -1
        lo, hi = 0, top
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fits(mid):
                lo = mid
            else:
                hi = mid - 1
        return lo

    for details in (True, False):
        text = build(len(rows), len(items), details)
        if len(text) <= limit:
            return text
    n = most(lambda k: len(build(len(rows), k, False)) <= limit, len(items))
    if n >= 0:
        return build(len(rows), n, False)
    n = most(lambda k: len(build(k, 0, False)) <= limit, len(rows))
    return build(max(n, 0), 0, False)[:limit]
