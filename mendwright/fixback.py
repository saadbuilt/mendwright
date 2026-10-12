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
- Never a key marked destructive, never a heal that sits inside an iframe, and never a key
  whose selectors came only from a seed file (there is no string in your code to replace).
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
import io
import os
import re
import stat
import tempfile
import time
import tokenize
from dataclasses import dataclass, field

from .selectors import CSS, XPATH, classify, normalize

# Folders that never hold your code. A folder is skipped by name, at any depth.
SKIP_DIRS = frozenset({
    '.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '.mendwright', 'build', 'dist',
    '__pycache__', '.tox', '.nox', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.eggs',
    'site-packages', '.claude',
})
DEFAULT_GLOBS = ('*.py',)
MAX_FILE_BYTES = 2 * 1024 * 1024        # larger files are not source code worth scanning

PYTHON_EXTS = ('.py', '.pyw', '.pyi')
# Languages with // and /* */ comments, where \' \" \\ escapes work in both quote styles.
C_LIKE_EXTS = frozenset({'.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts', '.java',
                         '.kt', '.kts', '.cs', '.groovy', '.scala', '.php', '.swift'})
ESCAPING_EXTS = C_LIKE_EXTS | {'.rb'}

_NEEDLE_SPLIT = re.compile(r'''['"\\\s]''')
_LINE = re.compile(r'[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+')
_WRITE_TRIES = 8

# An old selector that is one word ('button', 'input') equals strings that are not
# selectors (role='button', a dict entry), so a key with one is never written back.
_BARE_WORD = re.compile(r'^[A-Za-z][\w-]*$')
# Keys that heal.driver makes for driver.find_element(by, value), and that By. The healed
# selector is an XPath, which these Bys cannot run.
_DRIVER_KEYS = (('css selector=', 'By.CSS_SELECTOR'), ('id=', 'By.ID'), ('name=', 'By.NAME'))


# --- the plan: which keys to fix ---------------------------------------------------------

@dataclass
class Fix:
    """One key whose healed selector should replace its old code selectors."""
    key: str
    new: str                    # the healed selector (XPath)
    old: list                   # the key's active selectors from code, as stored
    confirmed: bool             # a canary success for this selector is in the journal
    role: str | None = None


@dataclass
class Plan:
    fixes: list = field(default_factory=list)
    skipped: list = field(default_factory=list)       # (key, reason): keys left alone
    new_for: dict = field(default_factory=dict)       # old selector -> new, when unambiguous
    keys_for: dict = field(default_factory=dict)      # old selector -> keys that use it
    ambiguous: dict = field(default_factory=dict)     # old selector -> keys, when ambiguous


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


def plan_fixes(store, events, *, include_unconfirmed=False):
    """The Plan for a store (schema-2 dict) and its journal events (oldest first, or an
    empty list). The key is fixed when its best active selector, the one that succeeded most
    recently, came from a heal."""
    selectors = store.get('selectors') if isinstance(store, dict) else None
    selectors = selectors if isinstance(selectors, dict) else {}
    canary = _canary_state(events)
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
        if not candidates:
            continue
        # The one that succeeded most recently. Timestamps have a one second resolution, so
        # a heal and the code selector it replaced can tie: the heal wins the tie, because
        # a heal was only made after the code selector stopped matching. A heal that never
        # matched anything is not a fix.
        best = sorted(candidates, key=lambda c: (_text(c.get('lastSucceededAt')),
                                                 c.get('source') == 'heal'), reverse=True)[0]
        if best.get('source') != 'heal' or not _text(best.get('lastSucceededAt')):
            continue
        new = best['xpath']
        if entry.get('destructive') is True:
            plan.skipped.append((key, 'it is marked destructive, so its heals are never written '
                                 'into code. Fix the selector by hand.'))
            continue
        if best.get('frameXPath'):
            plan.skipped.append((key, 'its healed selector runs inside the iframe ' + str(best['frameXPath'])
                                 + ', and a selector in your code runs in the page itself. Change '
                                 'the selector and the frame handling by hand.'))
            continue
        old = list(dict.fromkeys(c['xpath'] for c in candidates
                                 if c.get('source') == 'code' and c['xpath'] != new))
        if not old:
            plan.skipped.append((key, 'no selector from your code is saved for it (its selectors '
                                 'come from a seed file, or locator() was called without one), '
                                 'so there is no string in your code to replace. Update the seed '
                                 'file by hand, or pass the healed selector to heal.locator().'))
            continue
        bare = [s for s in old if _BARE_WORD.match(s)]
        if bare:
            plan.skipped.append((key, 'its old selector ' + repr(bare[0]) + ' is a bare word, and '
                                 'other strings in your code can be the same word (a role= '
                                 'value, a dict entry), so mendwright cannot tell which ones '
                                 'are the selector. Change it by hand to: ' + new))
            continue
        by = next((b for prefix, b in _DRIVER_KEYS if key.startswith(prefix)), None)
        if by and classify(new) == XPATH and all(classify(s) == CSS for s in old):
            plan.skipped.append((key, 'it comes from driver.find_element(' + by + ', ...) through '
                                 'heal.driver, and ' + by + ' cannot run the healed XPath. '
                                 'Change the call by hand to: find_element(By.XPATH, '
                                 + repr(new) + ')'))
            continue
        confirmed = canary.get((key, new)) is True
        if not confirmed and not include_unconfirmed:
            plan.skipped.append((key, 'its heal was never confirmed (journal.jsonl has no canary '
                                 'success for it; the pytest plugin or an expect= or confirm(True) '
                                 'check writes one in the run that makes the heal). Check that '
                                 'the selector finds the right element, then add '
                                 '--include-unconfirmed to write it anyway.'))
            continue
        role = entry.get('expectedRole')
        plan.fixes.append(Fix(key, new, old, confirmed, role if isinstance(role, str) else None))

    by_key = {fix.key: fix for fix in plan.fixes}
    for fix in plan.fixes:
        for selector in fix.old:
            others = claims.get(selector, set()) - {fix.key}
            blockers = [k for k in others
                        if not (k in by_key and selector in by_key[k].old
                                and by_key[k].new == fix.new)]
            plan.keys_for[selector] = sorted(others | {fix.key})
            if blockers:
                plan.ambiguous[selector] = plan.keys_for[selector]
                plan.new_for.pop(selector, None)
            elif selector not in plan.ambiguous:
                plan.new_for[selector] = fix.new
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


def collect_files(paths, extra_globs=()):
    """(files, errors): the source files under ``paths`` (folders are walked, a file path is
    taken as it is), in a stable order. Folders in SKIP_DIRS, symbolic links and Windows
    junctions are skipped."""
    globs = list(DEFAULT_GLOBS) + [g for g in extra_globs if g not in DEFAULT_GLOBS]
    files, errors, seen = [], [], set()

    def add(path):
        real = os.path.normcase(os.path.abspath(path))
        if real not in seen:
            seen.add(real)
            files.append(os.path.abspath(path))

    for root in paths:
        if os.path.isfile(root):
            add(root)
        elif os.path.isdir(root):
            for folder, dirs, names in os.walk(root):
                dirs[:] = sorted(d for d in dirs
                                 if d not in SKIP_DIRS and not d.endswith('.egg-info')
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
KEYED_CALLS = frozenset({'locator', 'interrupt'})
# Selenium's By names and the strings they stand for.
_BY_NAMES = {'XPATH': 'xpath', 'CSS_SELECTOR': 'css selector', 'ID': 'id', 'NAME': 'name',
             'CLASS_NAME': 'class name', 'TAG_NAME': 'tag name', 'LINK_TEXT': 'link text',
             'PARTIAL_LINK_TEXT': 'partial link text'}
_BY_VALUES = {v: k for k, v in _BY_NAMES.items()}
SKIP = 'skip'           # not a selector at all
CSS_ONLY = 'css'        # a selector, but in a place that cannot run an XPath
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


def python_contexts(text):
    """{(line, column): context} for the string literals of Python source that are not a
    plain selector. The context is SKIP for a literal that is no selector at all (a role=,
    text= or intent= value, or the key in heal.locator(key, selector)), or (CSS_ONLY, by)
    for one that sits where an XPath cannot run: the value next to a Selenium By other than
    By.XPATH (in a call or a (By, value) pair), or an argument of a function with css in its
    name. Columns count characters, as tokenize does. {} when the source does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    lines = _split_lines(text)
    out = {}

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
    errors: list = field(default_factory=list)
    written: list = field(default_factory=list)
    has_robot: bool = False


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
        result.notes.append(shown + ' was not changed: it could not be read as Python ('
                            + str(exc) + '). Fix the syntax error and run again, or edit the '
                            'selector by hand.')
        return found

    table = {**plan.new_for, **plan.ambiguous}
    healed = {fix.new: fix.new for fix in plan.fixes}
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
        if selector in plan.ambiguous:
            blocked.append((lit.line, selector, plan.ambiguous[selector]))
            continue
        new = plan.new_for[selector]
        if lit.value == new:
            continue
        if context and context[0] == CSS_ONLY and classify(new) == XPATH:
            result.notes.append(shown + ':' + str(lit.line) + ' was not changed: ' + lit.source
                                + ' is passed to ' + context[1] + ', which cannot run the healed '
                                'XPath. Change the call to take an XPath (By.XPATH in Selenium) '
                                'and the selector to: ' + new)
            continue
        now = python_literal(new, lit.source) if python else generic_literal(new, lit.source, ext)
        if now is None:
            result.notes.append(shown + ':' + str(lit.line) + ' was not changed: ' + lit.source
                                + ' is the old selector of ' + ', '.join(plan.keys_for[selector])
                                + ', but the healed selector cannot be written into this kind of '
                                'file without guessing at its escapes. Replace it by hand with: '
                                + new)
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
        needles = _needles(set(plan.new_for) | set(plan.ambiguous) | {f.new for f in plan.fixes})
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
            lines.append('  now  ' + fix.new)
            places = ['  ' + r.path + ':' + str(r.line) for c in result.changes
                      for r in c.replacements if fix.key in r.keys]
            lines.extend(places)
            if len(fix.old) > 1:
                lines.append('  (it had ' + str(len(fix.old)) + ' selectors in code, and each '
                             'found one is replaced by the healed selector)')

    left = [key + ': ' + reason for key, reason in result.plan.skipped]
    for blocked in result.blocked:
        selectors = sorted({i[1] for i in blocked.items})
        keys = sorted({k for i in blocked.items for k in i[2]})
        left.append(blocked.shown + ' was not touched: ' + _quote_list(selectors)
                    + (' belongs' if len(selectors) == 1 else ' belong') + ' to more than one key ('
                    + ', '.join(keys) + '), so mendwright cannot tell which healed selector goes at '
                    + _count(len(blocked.items), 'place')
                    + (' (line ' if len(blocked.items) == 1 else ' (lines ')
                    + ', '.join(str(i[0]) for i in blocked.items) + '). '
                    + ('The ' + _count(blocked.held_back, 'other replacement') + ' in this file '
                       + ('was' if blocked.held_back == 1 else 'were') + ' held back too. '
                       if blocked.held_back else '')
                    + 'Give each key its own selector string, or edit the file by hand.')
    left.extend(result.notes)
    for fix in result.unmatched:
        where = result.holding.get(fix.new)
        if where:
            left.append(fix.key + ': your code already passes the healed selector ('
                        + ', '.join(where[:3]) + (', and more' if len(where) > 3 else '')
                        + '), so there is nothing to change. Run your tests once and mendwright '
                        "records it as your code's selector.")
            continue
        left.append(fix.key + ': no string equal to ' + _quote_list(fix.old) + ' was found in '
                    + _count(result.scanned, 'file') + '. Add --path or --glob (for example --glob '
                    "'*.ts') if it is somewhere else, or change it by hand to: " + fix.new
                    + (' mendwright fix only rewrites quoted strings, so unquoted Robot Framework '
                       'arguments are not changed.' if result.has_robot else ''))
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
