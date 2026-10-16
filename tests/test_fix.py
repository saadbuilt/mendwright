"""`mendwright fix`: write healed selectors back into the code that passes the old ones.

Every test builds a small project in tmp_path (source files, an override store and a journal),
runs mendwright.cli.main([...]) in-process and reads the output and the files. Offline: no
network, no model, no browser.
"""

import ast
import json
import os
import stat
import sys
import textwrap

import pytest

from mendwright import cli, fixback

T0 = "2026-10-01T08:00:00+00:00"
T1 = "2026-10-02T09:30:00+00:00"

TERMS = "//label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox']"
EMAIL = "//input[@name='email' and @type='text']"
QUOTES = "//a[contains(.,\"Tom's\")]"


# --- building a project ------------------------------------------------------------------

def cand(xpath, source, last=None, status="active", frame=None):
    c = {"xpath": xpath, "source": source, "status": status, "addedAt": T0, "lastSucceededAt": last}
    if frame:
        c["frameXPath"] = frame
    return c


def healed(old, new, role="checkbox", **extra):
    """A key whose code passes ``old`` (a selector or a list of them) and whose best selector
    is the heal ``new``."""
    olds = [old] if isinstance(old, str) else list(old)
    entry = {"expectedRole": role,
             "candidates": [cand(o, "code", T0) for o in olds] + [cand(new, "heal", T1)]}
    entry.update(extra)
    return entry


def applied(key, new, old=(), ts=T1):
    return {"ts": ts, "event": "heal.applied", "key": key, "provider": "StubProvider",
            "model": "stub-model", "latency_ms": 12, "xpath": new, "old": list(old)}


def canary(key, xpath, success=True, ts=T1):
    return {"ts": ts, "event": "heal.canary", "key": key, "xpath": xpath, "success": success}


def write_store(root, selectors, events=()):
    """overrides.json (and journal.jsonl, unless events is None) in root/.mendwright."""
    folder = root / ".mendwright"
    folder.mkdir(parents=True, exist_ok=True)
    store = folder / "overrides.json"
    store.write_text(json.dumps({"schema": 2, "selectors": selectors}), encoding="utf-8")
    if events is not None:
        (folder / "journal.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return store


def confirmed_project(root, key, old, new, **extra):
    """A store with one confirmed heal."""
    entry = healed(old, new, **extra)
    return write_store(root, {key: entry}, [applied(key, new, [old] if isinstance(old, str) else old),
                                            canary(key, new)])


def write(root, name, text, newline="\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def run(capsys, *args):
    code = cli.main(["fix", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


# --- the dry run and --apply -------------------------------------------------------------

def test_dry_run_prints_a_diff_and_changes_nothing(root, capsys):
    confirmed_project(root, "accept_terms", "#accept-terms", TERMS)
    source = write(root, "tests/test_checkout.py",
                   'def test_it(heal):\n    heal.locator("accept_terms", "#accept-terms").check()\n')
    before = source.read_bytes()
    code, out, err = run(capsys)
    assert (code, err) == (0, "")
    assert source.read_bytes() == before
    lines = out.splitlines()
    assert lines[0] == ("mendwright fix: 1 replacement in 1 file would put 1 healed selector into "
                        "your code. Dry run, nothing was changed.")
    assert "--- a/tests/test_checkout.py" in lines
    assert "+++ b/tests/test_checkout.py" in lines
    assert '-    heal.locator("accept_terms", "#accept-terms").check()' in lines
    assert '+    heal.locator("accept_terms", "' + TERMS + '").check()' in lines
    assert "accept_terms  (checkbox)  healed, confirmed" in lines
    assert "  was  #accept-terms" in lines
    assert "  now  " + TERMS in lines
    assert "  tests/test_checkout.py:2" in lines
    assert "Run the same command with --apply to write these changes." in lines
    assert ("Once your code holds the new selector, mendwright tries it first on the next run, "
            "because selectors in your code win over saved heals.") in lines


def test_apply_writes_the_file_and_the_next_run_has_nothing_to_do(root, capsys):
    confirmed_project(root, "accept_terms", "#accept-terms", TERMS)
    source = write(root, "test_a.py", 'heal.locator("accept_terms", "#accept-terms")\n')
    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "mendwright fix: wrote 1 replacement in 1 file (1 key)."
    assert "Run the same command with --apply" not in out
    assert source.read_text(encoding="utf-8") == 'heal.locator("accept_terms", "' + TERMS + '")\n'
    assert [p.name for p in root.iterdir() if p.name.startswith(".mendwright-fix")] == []

    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "mendwright fix: nothing to write back."
    assert "accept_terms: your code already passes the healed selector (test_a.py:1), so there " \
           "is nothing to change." in out


def test_nothing_to_do_when_no_key_has_a_heal_in_front(root, capsys):
    write_store(root, {"save": {"expectedRole": "button", "candidates": [
        cand("#save", "code", T1), cand("//button[@id='old']", "heal", T0)]}}, [])
    write(root, "test_a.py", 'heal.locator("save", "#save")\n')
    code, out, err = run(capsys)
    assert (code, err) == (0, "")
    assert out.splitlines() == ["mendwright fix: nothing to write back.",
                                "No key has a healed selector ahead of the one in your code."]


def test_an_empty_store_has_nothing_to_do(root, capsys):
    write_store(root, {}, [])
    assert run(capsys) == (0, "mendwright fix: nothing to write back.\nNo key has a healed "
                              "selector ahead of the one in your code.\n", "")


# --- which keys are fixed ----------------------------------------------------------------

def test_an_unconfirmed_heal_is_left_out_until_asked_for(root, capsys):
    write_store(root, {"accept_terms": healed("#accept-terms", TERMS)},
                [applied("accept_terms", TERMS, ["#accept-terms"])])
    source = write(root, "test_a.py", 'heal.locator("accept_terms", "#accept-terms")\n')
    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    assert source.read_text(encoding="utf-8") == 'heal.locator("accept_terms", "#accept-terms")\n'
    assert ("  accept_terms: its heal was never confirmed (journal.jsonl has no canary success for "
            "it; the pytest plugin or an expect= or confirm(True) check writes one in the run "
            "that makes the heal). Check that the selector finds the right element, then add "
            "--include-unconfirmed to write it anyway.") in out.splitlines()

    code, out, err = run(capsys, "--apply", "--include-unconfirmed")
    assert (code, err) == (0, "")
    assert source.read_text(encoding="utf-8") == 'heal.locator("accept_terms", "' + TERMS + '")\n'
    assert "accept_terms  (checkbox)  healed, NOT confirmed" in out


def test_a_failed_canary_does_not_confirm_and_the_latest_one_counts(root, capsys):
    events = [applied("k", TERMS, ["#a"]), canary("k", TERMS, False), canary("k", TERMS, True)]
    write_store(root, {"k": healed("#a", TERMS)}, events)
    write(root, "test_a.py", 'heal.locator("k", "#a")\n')
    assert "healed, confirmed" in run(capsys)[1]
    write_store(root, {"k": healed("#a", TERMS)}, [applied("k", TERMS, ["#a"]),
                                                   canary("k", TERMS, True),
                                                   canary("k", TERMS, False)])
    assert "healed, confirmed" not in run(capsys)[1]


def test_without_a_journal_no_heal_is_confirmed(root, capsys):
    write_store(root, {"k": healed("#a", TERMS)}, events=None)
    write(root, "test_a.py", 'heal.locator("k", "#a")\n')
    out = run(capsys)[1]
    assert "k: its heal was never confirmed (journal.jsonl has no canary" in out
    assert "mendwright fix: nothing to write back." in out


def test_a_destructive_key_is_never_fixed(root, capsys):
    confirmed_project(root, "pay", "#pay", "//button[@id='pay-now']", role="button",
                      destructive=True)
    source = write(root, "test_a.py", 'heal.locator("pay", "#pay", destructive=True)\n')
    before = source.read_bytes()
    code, out, err = run(capsys, "--apply", "--include-unconfirmed")
    assert (code, err) == (0, "")
    assert source.read_bytes() == before
    assert ("  pay: it is marked destructive, so its heals are never written into code. Fix the "
            "selector by hand.") in out.splitlines()


def test_a_heal_inside_an_iframe_is_not_written_into_code(root, capsys):
    write_store(root, {"k": {"expectedRole": "button", "candidates": [
        cand("#a", "code", T0), cand("//button[@id='b']", "heal", T1, frame="//iframe[@id='shop']")]}},
        [canary("k", "//button[@id='b']")])
    source = write(root, "test_a.py", 'heal.locator("k", "#a")\n')
    before = source.read_bytes()
    out = run(capsys, "--apply")[1]
    assert source.read_bytes() == before
    assert "  k: its healed selector runs inside the iframe //iframe[@id='shop'], and a selector " \
           "in your code runs in the page itself." in out


def test_a_key_with_only_seed_selectors_has_no_code_to_fix(root, capsys):
    write_store(root, {"k": {"expectedRole": "button", "candidates": [
        cand("#a", "seed", T0), cand("//button[@id='b']", "heal", T1)]}},
        [canary("k", "//button[@id='b']")])
    source = write(root, "test_a.py", 'heal.locator("k", "#a")\n')
    before = source.read_bytes()
    out = run(capsys, "--apply")[1]
    assert source.read_bytes() == before
    assert "  k: no selector from your code is saved for it (its selectors come from a seed file" \
           in out


def test_superseded_and_quarantined_selectors_are_not_the_old_ones(root, capsys):
    entry = {"expectedRole": "button", "candidates": [
        cand("#old", "code", T0, status="superseded"),
        cand("#now", "code", T0),
        cand("//button[@id='q']", "heal", None, status="quarantined"),
        cand("//button[@id='fixed']", "heal", T1)]}
    write_store(root, {"k": entry}, [canary("k", "//button[@id='fixed']")])
    source = write(root, "test_a.py",
                   'heal.locator("k", "#now")\nheal.locator("other", "#old")\n')
    assert run(capsys, "--apply")[0] == 0
    assert source.read_text(encoding="utf-8") == (
        'heal.locator("k", "//button[@id=\'fixed\']")\nheal.locator("other", "#old")\n')


def test_the_best_heal_is_the_one_that_succeeded_last(root, capsys):
    entry = {"expectedRole": "button", "candidates": [
        cand("#a", "code", T0),
        cand("//button[@id='first']", "heal", "2026-10-02T00:00:00+00:00"),
        cand("//button[@id='second']", "heal", "2026-10-03T00:00:00+00:00")]}
    write_store(root, {"k": entry}, [canary("k", "//button[@id='second']")])
    source = write(root, "test_a.py", 'heal.locator("k", "#a")\n')
    assert run(capsys, "--apply")[0] == 0
    assert source.read_text(encoding="utf-8") == 'heal.locator("k", "//button[@id=\'second\']")\n'


def test_a_heal_that_ties_with_its_code_selector_wins_and_one_that_never_matched_does_not(
        root, capsys):
    new = "//button[@id='new']"
    tie = {"expectedRole": "button", "candidates": [cand("#a", "code", T1), cand(new, "heal", T1)]}
    write_store(root, {"tie": tie}, [canary("tie", new)])
    source = write(root, "test_a.py", 'x = "#a"\n')
    assert run(capsys, "--apply")[0] == 0
    assert source.read_text(encoding="utf-8") == 'x = "' + new + '"\n'

    never = {"expectedRole": "button", "candidates": [cand("#b", "code"), cand(new, "heal")]}
    write_store(root, {"never": never}, [canary("never", new)])
    other = write(root, "test_b.py", 'y = "#b"\n')
    out = run(capsys, "--apply", "--include-unconfirmed")[1]
    assert other.read_text(encoding="utf-8") == 'y = "#b"\n'
    assert out.splitlines()[0] == "mendwright fix: nothing to write back."


LIST_TILE = "//div[contains(concat(' ', normalize-space(@class), ' '), ' tile ')]"


@pytest.mark.parametrize('first_card', ["2026-10-02T09:30:00+00:00", "2026-10-02T09:30:07+00:00"])
def test_a_key_with_a_list_heal_and_a_heal_of_one_element_is_left_alone(root, capsys, first_card):
    # heal.locator_all('.card', '.card') and heal.page.inner_text('.card') share a key, and
    # each healed: one string in the code cannot take both, whichever succeeded last.
    entry = {"expectedRole": "div", "candidates": [
        cand(".card", "code", T0), dict(cand(LIST_TILE, "heal", T1), list=True, css="div.tile"),
        dict(cand("//div[@id='p1']", "heal", first_card), css="#p1")]}
    write_store(root, {".card": entry}, [canary(".card", LIST_TILE), canary(".card", "//div[@id='p1']")])
    text = 'cards = page.locator(".card").all()\nfirst = page.inner_text(".card")\n'
    source = write(root, "test_a.py", text)
    code, out, _err = run(capsys, "--apply")
    assert code == 0 and source.read_text(encoding="utf-8") == text
    assert out.splitlines()[0] == "mendwright fix: nothing to write back."
    assert ("  .card: it has a heal of the list (heal.locator_all()) and a heal of one element "
            "under the same key, and one selector in your code cannot be both, so mendwright "
            "cannot tell which one goes where. Change the selectors by hand, or give the list "
            "a key of its own: the list to div.tile, the element to #p1") in out.splitlines()


def test_a_key_whose_heal_is_a_list_heal_is_left_alone(root, capsys):
    # Only a build of 0.8.0 before its release saved list heals, and this one is the items
    # saved for later that a cart's lookup took once the cart was emptied.
    saved = "//div[contains(concat(' ', normalize-space(@class), ' '), ' saved-item ')]"
    entry = {"expectedRole": "div", "candidates": [
        cand(".cart-item", "code", T0), dict(cand(saved, "heal", T1), list=True, css="div.saved-item")]}
    write_store(root, {".cart-item": entry}, [canary(".cart-item", saved)])
    text = 'for row in heal.locator_all(".cart-item", ".cart-item"):\n    row.click()\n'
    source = write(root, "test_a.py", text)
    code, out, _err = run(capsys, "--apply")
    assert code == 0 and source.read_text(encoding="utf-8") == text
    assert out.splitlines()[0] == "mendwright fix: nothing to write back."
    assert ("  .cart-item: its heal is a list heal saved by a build of 0.8.0 before its release, "
            "which nobody checked: it can be another list that holds the items of an emptied one. "
            "If the selector broke, change it by hand, or run the test with nonempty=True to have "
            "one proposed.") in out.splitlines()


def test_a_heal_of_one_element_is_never_written_into_a_list_s_key(root, capsys):
    # heal.locator_all('.cart-item', '.cart-item') recorded the list, and
    # heal.page.inner_text('.cart-item') healed onto one row saved for later once the cart
    # was emptied: written back, the cart's loop would take that row.
    saved = "//div[@id='saved-1']"
    entry = {"expectedRole": "div", "listFingerprint": {"version": 1},
             "candidates": [cand(".cart-item", "code", T0), dict(cand(saved, "heal", T1), css="#saved-1")]}
    write_store(root, {".cart-item": entry}, [canary(".cart-item", saved)])
    text = ('for row in heal.locator_all(".cart-item", ".cart-item"):\n    row.click()\n'
            'first = heal.page.inner_text(".cart-item")\n')
    source = write(root, "test_a.py", text)
    code, out, _err = run(capsys, "--apply")
    assert code == 0 and source.read_text(encoding="utf-8") == text
    assert out.splitlines()[0] == "mendwright fix: nothing to write back."
    assert ("  .cart-item: heal.locator_all() looks it up as a list, and its heal is of one "
            "element, so written into the list call it would make the list match that one "
            "element. Give the list a key of its own, or change the selector by hand to: "
            + saved) in out.splitlines()


def test_every_old_code_selector_of_a_key_is_replaced(root, capsys):
    confirmed_project(root, "k", ["#a", "//button[@id='b']"], EMAIL, role="input")
    source = write(root, "test_a.py", "heal.locator('k', ['#a', \"//button[@id='b']\"])\n")
    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    assert source.read_text(encoding="utf-8") == (
        'heal.locator(\'k\', ["' + EMAIL + '", "' + EMAIL + '"])\n')
    assert "(it had 2 selectors in code, and each found one is replaced by the healed selector)" in out


# --- string literals ---------------------------------------------------------------------

def rewrite(root, capsys, line, old="#save", new=TERMS):
    """Fix a one-line Python file and return the new line, or None when it was left alone."""
    confirmed_project(root, "k", old, new)
    source = write(root, "test_a.py", line + "\n")
    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    ast.parse(source.read_text(encoding="utf-8"))
    after = source.read_text(encoding="utf-8").rstrip("\n")
    return None if after == line else after


@pytest.mark.parametrize("line, new, expected", [
    ('x = "#save"', "//input[@id='a']", 'x = "//input[@id=\'a\']"'),
    ("x = '#save'", "//input[@id='a']", 'x = "//input[@id=\'a\']"'),
    ('x = "#save"', '//a[@id="b"]', "x = '//a[@id=\"b\"]'"),
    ("x = '#save'", '//a[@id="b"]', "x = '//a[@id=\"b\"]'"),
    ('x = "#save"', '//a[contains(.,"Tom\'s")]', 'x = "//a[contains(.,\\"Tom\'s\\")]"'),
    ("x = '#save'", '//a[contains(.,"Tom\'s")]', "x = '//a[contains(.,\"Tom\\'s\")]'"),
    ('x = """#save"""', '//a[contains(.,"Tom\'s")]', 'x = """//a[contains(.,"Tom\'s")]"""'),
    ("x = '''#save'''", '//a[contains(.,"Tom\'s")]', "x = '''//a[contains(.,\"Tom's\")]'''"),
    ('x = """#save"""', 'a"', "x = '''a\"'''"),
    ("x = '''#save'''", "a'", "x = \"\"\"a'\"\"\""),
    ('x = r"#save"', '//a[@href="x"]', "x = r'//a[@href=\"x\"]'"),
    ('x = r"#save"', "//a[@d='\\d']", 'x = r"//a[@d=\'\\d\']"'),
    ('x = r"#save"', "//a['\"\\d']", 'x = "//a[\'\\"\\\\d\']"'),
    ('x = R"#save"', "a\\", 'x = "a\\\\"'),
    ('x = u"#save"', "//a[@id='x']", 'x = u"//a[@id=\'x\']"'),
    ('x = "#save"', "a\\b", 'x = "a\\\\b"'),
    ('x = "#save"', "a\nb\tc", 'x = "a\\nb\\tc"'),
], ids=["double-single", "single-single", "double-double", "single-double", "double-both",
        "single-both", "triple-both", "triple-single-both", "triple-trailing-quote",
        "triple-trailing-single", "raw-switch", "raw-keeps-backslash", "raw-both-quotes",
        "raw-trailing-backslash", "u-prefix", "backslash", "control-characters"])
def test_the_new_literal_is_valid_python_with_the_right_value(root, capsys, line, new, expected):
    assert rewrite(root, capsys, line, new=new) == expected
    value = ast.literal_eval(expected[len("x = "):])
    assert value == new


def test_python_literal_always_evaluates_to_the_selector():
    selectors = [TERMS, QUOTES, "a\\", "\\", "'", '"', "'''", '"""', "x'''", 'x"""', "a\nb", "é",
                 "\x00", "\u2028", "\U0001f600", "\\'", "//a[@d='\\\\']"]
    tokens = ['"#x"', "'#x'", '"""#x"""', "'''#x'''", 'r"#x"', "r'#x'", 'u"#x"', 'R"""#x"""',
              "r'''#x'''"]
    for new in selectors:
        for token in tokens:
            literal = fixback.python_literal(new, token)
            assert ast.literal_eval(literal) == new, (new, token, literal)


def test_an_f_string_bytes_a_comment_and_a_substring_are_left_alone(root, capsys):
    source = textwrap.dedent('''\
        a = f"#save"
        b = f"{d['#save']}"
        c = f'{"#save"}'
        d = b"#save"
        e = "#save-all"
        f = "click #save now"
        g = "#sa" "ve"
        # "#save" in a comment
        h = rf"#save"
        i = ("#save"
             "x")
        j = "x" \\
            "#save"
    ''')
    confirmed_project(root, "k", "#save", TERMS)
    path = write(root, "test_a.py", source)
    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    assert path.read_text(encoding="utf-8") == source
    assert "k: no string equal to '#save' was found in 1 file." in out


def test_only_a_whole_string_is_replaced_and_every_copy_is(root, capsys):
    source = textwrap.dedent('''\
        A = "#save"
        B = ["#save", '#save', """#save"""]
        C = {"#save": 1}
        D = foo("#save", x="#save")
    ''')
    confirmed_project(root, "k", "#save", "//b[@id='s']")
    path = write(root, "test_a.py", source)
    assert run(capsys, "--apply")[0] == 0
    assert path.read_text(encoding="utf-8") == textwrap.dedent('''\
        A = "//b[@id='s']"
        B = ["//b[@id='s']", "//b[@id='s']", """//b[@id='s']"""]
        C = {"//b[@id='s']": 1}
        D = foo("//b[@id='s']", x="//b[@id='s']")
    ''')


def test_a_stored_spelling_matches_the_literal_that_normalizes_to_it(root, capsys):
    confirmed_project(root, "k", ["//a[@id='x']", "#save"], "//b[@id='y']", role="button")
    source = write(root, "test_a.py", 'A = "xpath=//a[@id=\'x\']"\nB = " #save "\nC = "css=#save"\n')
    assert run(capsys, "--apply")[0] == 0
    assert source.read_text(encoding="utf-8") == 'A = "//b[@id=\'y\']"\nB = "//b[@id=\'y\']"\nC = "//b[@id=\'y\']"\n'


def test_offsets_are_right_after_wide_characters_form_feeds_and_unicode_separators(root, capsys):
    source = ('x = "\u00e9\U0001f600\u2028"; y = "#save"; z = "\u00e9"\n'
              '\x0c\n'
              'heal.locator("k", "#save")  # \u2028 in a comment\n')
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    path = write(root, "test_a.py", source)
    assert run(capsys, "--apply")[0] == 0
    assert path.read_text(encoding="utf-8") == source.replace("#save", "//b[@id='y']")


def test_crlf_and_bom_and_mixed_endings_are_kept(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    crlf = write(root, "crlf.py", 'a = 1\nb = "#save"\nc = """x\ny"""\n', newline="\r\n")
    bom = root / "bom.py"
    bom.write_bytes(b"\xef\xbb\xbfa = '#save'\r\nb = 2\n")
    mixed = root / "mixed.py"
    mixed.write_bytes(b"a = 1\r\nb = '#save'\nc = 3\r")
    before = (crlf.read_bytes(), bom.read_bytes(), mixed.read_bytes())
    assert run(capsys)[0] == 0
    assert (crlf.read_bytes(), bom.read_bytes(), mixed.read_bytes()) == before
    assert run(capsys, "--apply")[0] == 0
    assert crlf.read_bytes() == b'a = 1\r\nb = "//b[@id=\'y\']"\r\nc = """x\r\ny"""\r\n'
    assert bom.read_bytes() == b"\xef\xbb\xbfa = \"//b[@id='y']\"\r\nb = 2\n"
    assert mixed.read_bytes() == b"a = 1\r\nb = \"//b[@id='y']\"\nc = 3\r"


def test_a_coding_comment_is_honoured_and_the_encoding_kept(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    path = root / "latin.py"
    path.write_bytes("# -*- coding: latin-1 -*-\nx = '\u00e9'; y = '#save'\n".encode("latin-1"))
    assert run(capsys, "--apply")[0] == 0
    assert path.read_bytes() == "# -*- coding: latin-1 -*-\nx = '\u00e9'; y = \"//b[@id='y']\"\n".encode("latin-1")


def test_a_file_that_is_not_valid_python_is_left_alone_and_named(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    broken = write(root, "broken.py", 'def f(:\n    x = "#save"\n')
    fine = write(root, "fine.py", 'x = "#save"\n')
    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    assert broken.read_text(encoding="utf-8") == 'def f(:\n    x = "#save"\n'
    assert fine.read_text(encoding="utf-8") == 'x = "//b[@id=\'y\']"\n'
    assert "broken.py was not changed: it could not be read as Python (" in out


def test_python_literals_finds_plain_strings_only():
    source = textwrap.dedent('''\
        a = "one"
        b = f"two {x} {'inner'}"
        c = b"three"
        d = "four" "five"
        e = ("six"
             "seven")
        f = 'eight'
        g = """nine"""
        h = r'ten'
    ''')
    found = [lit.value for lit in fixback.python_literals(source)]
    assert found == ["one", "eight", "nine", "ten"]
    for lit in fixback.python_literals(source):
        assert source[lit.start:lit.end] == lit.source


# --- strings that equal the selector but must not change ---------------------------------

def test_a_key_or_a_role_value_equal_to_the_selector_is_not_rewritten(root, capsys):
    # heal.page saves a selector under itself as the key; the key and role= are not
    # selectors, so only the selector argument changes.
    new = "//button[@data-testid='login']"
    confirmed_project(root, "#login", "#login", new, role="button")
    source = write(root, "test_a.py", textwrap.dedent('''\
        heal.locator("#login", "#login").click()
        heal.locator("#login", selector="#login", text="#login")
        heal.interrupt("#login", "#login", action="click")
        heal.locator_all("#login", "#login")
        page.locator("#login").click()
    '''))
    assert run(capsys, "--apply")[0] == 0
    assert source.read_text(encoding="utf-8") == textwrap.dedent('''\
        heal.locator("#login", "{0}").click()
        heal.locator("#login", selector="{0}", text="#login")
        heal.interrupt("#login", "{0}", action="click")
        heal.locator_all("#login", "{0}")
        page.locator("{0}").click()
    ''').format(new)


def test_a_key_whose_old_selector_is_a_bare_word_is_left_alone(root, capsys):
    confirmed_project(root, "go", "button", "//button[@id='go']", role="button")
    source = write(root, "test_a.py", 'heal.locator("go", "button", role="button")\n'
                                      'KIND = {"kind": "button"}\n')
    before = source.read_bytes()
    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    assert source.read_bytes() == before
    assert ("  go: its old selector 'button' is a bare word, and other strings in your code can "
            "be the same word (a role= value, a dict entry), so mendwright cannot tell which ones "
            "are the selector. Change it by hand to: //button[@id='go']") in out.splitlines()


@pytest.mark.parametrize("key, by", [("css selector=#login", "By.CSS_SELECTOR"),
                                     ('id=login', "By.ID"), ('name=login', "By.NAME")])
def test_a_heal_driver_key_for_a_css_id_or_name_lookup_is_left_alone(root, capsys, key, by):
    old = {"By.CSS_SELECTOR": "#login", "By.ID": '[id="login"]', "By.NAME": '[name="login"]'}[by]
    new = "//button[@data-testid='login']"
    confirmed_project(root, key, old, new, role="button")
    source = write(root, "test_a.py", 'driver.find_element(' + by + ', "#login").click()\n')
    before = source.read_bytes()
    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    assert source.read_bytes() == before
    assert ("  " + key + ": it comes from driver.find_element(" + by + ", ...) through heal.driver, "
            "and " + by + " cannot run the healed XPath. Change the call by hand to: "
            "find_element(By.XPATH, \"" + new + "\")") in out.splitlines()


def test_a_selector_passed_where_only_css_runs_is_left_alone_and_listed(root, capsys):
    new = "//button[@data-testid='save']"
    confirmed_project(root, "save", "#save", new, role="button")
    source = write(root, "test_a.py", textwrap.dedent('''\
        heal.locator("save", "#save").click()
        driver.find_element(By.CSS_SELECTOR, "#save").click()
        driver.find_element(
            By.CSS_SELECTOR,
            "#save",
        )
        SAVE = (By.CSS_SELECTOR, "#save")
        driver.find_element(by="css selector", value="#save")
        driver.find_element_by_css_selector("#save")
        XPATH_ONE = (By.XPATH, "#save")
    '''))
    java = write(root, "src/SaveTest.java", textwrap.dedent('''\
        driver.findElement(By.cssSelector("#save")).click();
        page.locator("#save").click();
        document.querySelector('#save');
    '''))
    code, out, err = run(capsys, "--apply", "--glob", "*.java")
    assert (code, err) == (0, "")
    assert source.read_text(encoding="utf-8") == textwrap.dedent('''\
        heal.locator("save", "{0}").click()
        driver.find_element(By.CSS_SELECTOR, "#save").click()
        driver.find_element(
            By.CSS_SELECTOR,
            "#save",
        )
        SAVE = (By.CSS_SELECTOR, "#save")
        driver.find_element(by="css selector", value="#save")
        driver.find_element_by_css_selector("#save")
        XPATH_ONE = (By.XPATH, "{0}")
    ''').format(new)
    assert java.read_text(encoding="utf-8") == textwrap.dedent('''\
        driver.findElement(By.cssSelector("#save")).click();
        page.locator("{0}").click();
        document.querySelector('#save');
    ''').format(new)
    lines = out.splitlines()
    for where, by in (("test_a.py:2", "By.CSS_SELECTOR"), ("test_a.py:5", "By.CSS_SELECTOR"),
                      ("test_a.py:7", "By.CSS_SELECTOR"), ("test_a.py:8", "By.CSS_SELECTOR"),
                      ("test_a.py:9", "find_element_by_css_selector()"),
                      ("src/SaveTest.java:1", "cssSelector"),
                      ("src/SaveTest.java:3", "querySelector")):
        assert any(line.startswith("  " + where + " was not changed: ") and " is passed to " + by
                   + ", which cannot run the healed XPath. Change the call to take an XPath "
                   "(By.XPATH in Selenium) and the selector to: " + new in line
                   for line in lines), (where, out)


# --- ambiguity ---------------------------------------------------------------------------

def test_a_string_shared_by_two_keys_leaves_the_whole_file_alone_and_lists_it(root, capsys):
    write_store(root, {
        "checkout_submit": healed("#submit", "//button[@id='checkout']", role="button"),
        "signup_submit": healed("#submit", "//button[@id='signup']", role="button"),
        "terms": healed("#terms", TERMS),
    }, [canary("checkout_submit", "//button[@id='checkout']"),
        canary("signup_submit", "//button[@id='signup']"), canary("terms", TERMS)])
    shared = write(root, "tests/pages.py", 'A = "#submit"\nB = "#terms"\nC = "#submit"\n')
    clean = write(root, "tests/other.py", 'T = "#terms"\n')
    before = shared.read_bytes()
    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    assert shared.read_bytes() == before                      # untouched, with its fixable #terms
    assert clean.read_text(encoding="utf-8") == 'T = "' + TERMS + '"\n'
    assert ("  tests/pages.py was not touched: '#submit' belongs to more than one key "
            "(checkout_submit, signup_submit), so mendwright cannot tell which healed selector "
            "goes at 2 places (lines 1, 3). The 1 other replacement in this file was held back "
            "too. Give each key its own selector string, or edit the file by hand.") \
        in out.splitlines()
    assert "--- a/tests/other.py" in out.splitlines()
    assert "a/tests/pages.py" not in out


def test_a_working_key_that_uses_the_same_string_blocks_the_fix(root, capsys):
    write_store(root, {
        "broken": healed("#go", "//button[@id='new']", role="button"),
        "fine": {"expectedRole": "button", "candidates": [cand("#go", "code", T1)]},
    }, [canary("broken", "//button[@id='new']")])
    source = write(root, "test_a.py", 'heal.locator("broken", "#go")\nheal.locator("fine", "#go")\n')
    before = source.read_bytes()
    out = run(capsys, "--apply")[1]
    assert source.read_bytes() == before
    assert "test_a.py was not touched: '#go' belongs to more than one key (broken, fine)" in out


def test_two_keys_healed_to_the_same_selector_do_not_clash(root, capsys):
    new = "//button[@id='new']"
    write_store(root, {"a": healed("#go", new, role="button"), "b": healed("#go", new, role="button")},
                [canary("a", new), canary("b", new)])
    source = write(root, "test_a.py", 'x = "#go"\n')
    assert run(capsys, "--apply")[0] == 0
    assert source.read_text(encoding="utf-8") == 'x = "//button[@id=\'new\']"\n'


def test_a_selector_that_is_a_seed_selector_of_another_key_is_ambiguous(root, capsys):
    write_store(root, {
        "a": healed("#go", "//button[@id='new']", role="button"),
        "b": {"expectedRole": "button", "candidates": [cand("#go", "seed", T0)]},
    }, [canary("a", "//button[@id='new']")])
    source = write(root, "test_a.py", 'x = "#go"\n')
    out = run(capsys, "--apply")[1]
    assert source.read_text(encoding="utf-8") == 'x = "#go"\n'
    assert "belongs to more than one key (a, b)" in out


# --- finding files -----------------------------------------------------------------------

def test_folders_that_are_not_your_code_are_skipped(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    skipped = [".git/hooks/x.py", ".venv/lib/x.py", "venv/x.py", "node_modules/pkg/x.py",
               "build/x.py", "dist/x.py", "src/.mendwright/x.py", "__pycache__/x.py",
               "pkg.egg-info/x.py", ".claude/worktrees/w/x.py", "a/site-packages/x.py"]
    for name in skipped:
        write(root, name, 'x = "#save"\n')
    kept = write(root, "tests/x.py", 'x = "#save"\n')
    assert run(capsys, "--apply")[0] == 0
    for name in skipped:
        assert (root / name).read_text(encoding="utf-8") == 'x = "#save"\n', name
    assert kept.read_text(encoding="utf-8") == 'x = "//b[@id=\'y\']"\n'


def test_path_can_repeat_and_can_name_a_file_and_defaults_to_the_current_folder(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    for name in ("one/a.py", "two/b.py", "three/c.py", "four/d.txt"):
        write(root, name, 'x = "#save"\n')
    assert run(capsys, "--apply", "--path", "one", "--path", "two")[0] == 0
    assert (root / "one/a.py").read_text(encoding="utf-8") != 'x = "#save"\n'
    assert (root / "two/b.py").read_text(encoding="utf-8") != 'x = "#save"\n'
    assert (root / "three/c.py").read_text(encoding="utf-8") == 'x = "#save"\n'
    assert run(capsys, "--apply", "--path", "four/d.txt")[0] == 0     # a named file is taken as is
    assert (root / "four/d.txt").read_text(encoding="utf-8") == 'x = "//b[@id=\'y\']"\n'
    assert run(capsys, "--apply")[0] == 0                              # the default is '.'
    assert (root / "three/c.py").read_text(encoding="utf-8") == 'x = "//b[@id=\'y\']"\n'


def test_a_missing_path_is_an_error_and_nothing_is_changed(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    source = write(root, "a.py", 'x = "#save"\n')
    code, out, err = run(capsys, "--apply", "--path", "a.py", "--path", "nope")
    assert code == 1
    assert out == ""
    assert err == ("mendwright fix: no such file or folder: nope. Check the --path value; "
                   "nothing was changed.\n")
    assert source.read_text(encoding="utf-8") == 'x = "#save"\n'


def test_symbolic_links_are_not_followed(root, capsys, tmp_path):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = write(outside, "x.py", 'x = "#save"\n')
    try:
        os.symlink(str(outside), str(root / "link"), target_is_directory=True)
        os.symlink(str(target), str(root / "file_link.py"))
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    assert run(capsys, "--apply")[0] == 0
    assert target.read_text(encoding="utf-8") == 'x = "#save"\n'


def test_a_windows_junction_is_not_followed_out_of_the_path(root, capsys, tmp_path):
    # os.path.islink() is False for a junction, and os.walk() would go through it.
    winapi = pytest.importorskip("_winapi")
    if not hasattr(winapi, "CreateJunction"):
        pytest.skip("directory junctions are Windows only")
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = write(outside, "test_x.py", 'x = "#save"\n')
    winapi.CreateJunction(str(outside), str(root / "link"))
    try:
        files, errors = fixback.collect_files([str(root)])
        assert files == [] and errors == []
        assert run(capsys, "--apply")[0] == 0
        assert target.read_text(encoding="utf-8") == 'x = "#save"\n'
    finally:
        os.rmdir(str(root / "link"))           # removes the junction, not its target


def test_the_store_is_found_like_report_finds_it(root, capsys, tmp_path, monkeypatch):
    store = confirmed_project(tmp_path / "elsewhere", "k", "#save", "//b[@id='y']")
    write(root, "a.py", 'x = "#save"\n')
    code, out, err = run(capsys)                       # no store in ./.mendwright
    default = root / ".mendwright" / "overrides.json"
    assert (code, out) == (1, "")
    assert err == ("mendwright fix: no store at " + str(default) + ". Run your tests with "
                   "mendwright first, or pass --store PATH (the default is MENDWRIGHT_STORE, else "
                   "./.mendwright/overrides.json).\n")
    code, out, err = run(capsys, "--store", str(store))
    assert (code, err) == (0, "")
    assert out.startswith("mendwright fix: 1 replacement in 1 file")
    monkeypatch.setenv("MENDWRIGHT_STORE", str(store))
    code, out, err = run(capsys)
    assert (code, err) == (0, "")
    assert out.startswith("mendwright fix: 1 replacement in 1 file")
    assert run(capsys, "--store", str(tmp_path / "nope.json"))[2] == (
        "mendwright fix: no store at " + str(tmp_path / "nope.json") + ". Check the path given to "
        "--store; the file is created by the first heal.\n")


def test_an_unreadable_store_is_an_error(root, capsys):
    bad = root / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    code, out, err = run(capsys, "--store", str(bad))
    assert (code, out) == (1, "")
    assert err.startswith("mendwright fix: " + str(bad) + " is not valid JSON (")


def test_a_journal_with_junk_lines_is_read(root, capsys):
    write_store(root, {"k": healed("#a", TERMS)}, [])
    (root / ".mendwright" / "journal.jsonl").write_text(
        "{broken\n[1]\n" + json.dumps({"event": "heal.canary", "key": "k"}) + "\n"
        + json.dumps(canary("k", TERMS)) + "\n\n", encoding="utf-8")
    write(root, "a.py", 'x = "#a"\n')
    assert "healed, confirmed" in run(capsys)[1]


# --- other file types --------------------------------------------------------------------

def test_glob_adds_file_types_and_quoted_strings_are_replaced(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    ts = write(root, "e2e/page.ts", textwrap.dedent('''\
        const a = page.locator('#save');
        const b = page.locator("#save");
        const c = page.locator(`#save`);
        const d = page.locator('#save-all');   // not the same string
        const e = `#save${n}`;
        // page.locator('#save') in a line comment
        /* page.locator('#save')
           still a comment '#save' */
        const f = "don't"; const g = '#save';
        const h = `
            #save`;
    '''))
    robot = write(root, "e2e/login.robot", 'Click Element    "#save"\nClick Element    #save\n')
    skipped_py = write(root, "e2e/note.txt", 'x = "#save"\n')
    code, out, err = run(capsys, "--apply", "--glob", "*.ts", "*.robot")
    assert (code, err) == (0, "")
    assert ts.read_text(encoding="utf-8") == textwrap.dedent('''\
        const a = page.locator("//b[@id='y']");
        const b = page.locator("//b[@id='y']");
        const c = page.locator(`//b[@id='y']`);
        const d = page.locator('#save-all');   // not the same string
        const e = `#save${n}`;
        // page.locator('#save') in a line comment
        /* page.locator('#save')
           still a comment '#save' */
        const f = "don't"; const g = "//b[@id='y']";
        const h = `
            #save`;
    ''')
    assert robot.read_text(encoding="utf-8") == 'Click Element    "//b[@id=\'y\']"\nClick Element    #save\n'
    assert skipped_py.read_text(encoding="utf-8") == 'x = "#save"\n'


def test_glob_can_repeat_and_match_on_a_path(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    for name in ("a/x.ts", "b/y.js", "c/z.rb", "tests/e2e/w.js"):
        write(root, name, "x = '#save'\n")
    assert run(capsys, "--apply", "--glob", "*.ts", "--glob", "tests/*/*.js")[0] == 0
    assert (root / "a/x.ts").read_text(encoding="utf-8") != "x = '#save'\n"
    assert (root / "tests/e2e/w.js").read_text(encoding="utf-8") != "x = '#save'\n"
    assert (root / "b/y.js").read_text(encoding="utf-8") == "x = '#save'\n"
    assert (root / "c/z.rb").read_text(encoding="utf-8") == "x = '#save'\n"


def test_a_selector_with_both_quote_kinds_is_escaped_where_the_language_allows_it(root, capsys):
    confirmed_project(root, "k", "#save", QUOTES)
    ts = write(root, "page.ts", "const a = '#save';\n")
    rb = write(root, "page.rb", "a = '#save'\n")
    code, out, err = run(capsys, "--apply", "--glob", "*.ts", "*.rb")
    assert (code, err) == (0, "")
    assert ts.read_text(encoding="utf-8") == "const a = '//a[contains(.,\"Tom\\'s\")]';\n"
    assert rb.read_text(encoding="utf-8") == "a = '//a[contains(.,\"Tom\\'s\")]'\n"


def test_a_selector_that_needs_unknown_escapes_is_listed_not_guessed(root, capsys):
    confirmed_project(root, "k", "#save", QUOTES)
    robot = write(root, "login.robot", "Click Element    '#save'\n")
    code, out, err = run(capsys, "--apply", "--glob", "*.robot")
    assert (code, err) == (0, "")
    assert robot.read_text(encoding="utf-8") == "Click Element    '#save'\n"
    assert "login.robot:1 was not changed: '#save' is the old selector of k, but the healed " \
           "selector cannot be written into this kind of file without guessing at its " \
           "escapes. Replace it by hand with: " + QUOTES in out


def test_unquoted_robot_arguments_are_reported_as_not_changed(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    robot = write(root, "login.robot", "Click Element    #save\nClick Element    xpath=#save\n")
    out = run(capsys, "--apply", "--glob", "*.robot")[1]
    assert robot.read_text(encoding="utf-8") == "Click Element    #save\nClick Element    xpath=#save\n"
    assert "mendwright fix only rewrites quoted strings, so unquoted Robot Framework arguments " \
           "are not changed." in out


def test_generic_literals_in_a_utf8_file_with_bom(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    path = root / "page.ts"
    path.write_bytes(b"\xef\xbb\xbfconst a = '#save';\r\nconst b = 1;\r\n")
    assert run(capsys, "--apply", "--glob", "*.ts")[0] == 0
    assert path.read_bytes() == b"\xef\xbb\xbfconst a = \"//b[@id='y']\";\r\nconst b = 1;\r\n"


def test_binary_and_big_files_are_skipped(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    binary = root / "data.py"
    binary.write_bytes(b'x = "#save"\0\x01\x02')
    big = root / "big.py"
    big.write_text('x = "#save"\n' + "#" * (fixback.MAX_FILE_BYTES + 10), encoding="utf-8")
    latin = root / "latin.ts"
    latin.write_bytes(b"const a = '#save'; // \xe9\n")
    assert run(capsys, "--apply", "--glob", "*.ts")[0] == 0
    assert binary.read_bytes() == b'x = "#save"\0\x01\x02'
    assert big.read_text(encoding="utf-8").startswith('x = "#save"')
    assert latin.read_bytes() == b"const a = '#save'; // \xe9\n"


# --- when a file cannot be rewritten safely ----------------------------------------------

def test_a_rewrite_that_would_not_parse_is_an_error_and_writes_nothing(root, capsys, monkeypatch):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    source = write(root, "a.py", 'x = "#save"\n')
    monkeypatch.setattr(fixback, "python_literal", lambda new, token: '"broken')
    code, out, err = run(capsys, "--apply")
    assert code == 1
    assert source.read_text(encoding="utf-8") == 'x = "#save"\n'
    assert "mendwright fix: a.py was not changed: the file would no longer parse (" in err
    assert "edit the selector by hand and report it." in err


def test_a_rewrite_that_changes_the_wrong_value_is_caught(root, capsys, monkeypatch):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    source = write(root, "a.py", 'x = "#save"\n')
    monkeypatch.setattr(fixback, "python_literal", lambda new, token: '"//something-else"')
    code, out, err = run(capsys, "--apply")
    assert code == 1
    assert source.read_text(encoding="utf-8") == 'x = "#save"\n'
    assert "the strings that changed are not the ones that were planned" in err


def test_a_rewrite_that_changes_the_structure_is_caught(root, capsys, monkeypatch):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    source = write(root, "a.py", 'x = ["#save", 1]\n')
    monkeypatch.setattr(fixback, "python_literal", lambda new, token: '"//b", 2')
    code, out, err = run(capsys, "--apply")
    assert code == 1
    assert source.read_text(encoding="utf-8") == 'x = ["#save", 1]\n'
    assert "the change would alter more than the selector strings" in err


def test_one_bad_file_does_not_stop_the_others_and_exit_is_1(root, capsys, monkeypatch):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    bad = write(root, "a_bad.py", 'x = "#save"\n')
    good = write(root, "b_good.ts", "const x = '#save';\n")
    monkeypatch.setattr(fixback, "python_literal", lambda new, token: '"broken')
    code, out, err = run(capsys, "--apply", "--glob", "*.ts")
    assert code == 1
    assert bad.read_text(encoding="utf-8") == 'x = "#save"\n'
    assert good.read_text(encoding="utf-8") == "const x = \"//b[@id='y']\";\n"


def test_a_write_that_fails_leaves_the_file_as_it_was(root, capsys, monkeypatch):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    source = write(root, "a.py", 'x = "#save"\n')

    def refuse(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", refuse)
    monkeypatch.setattr(fixback.time, "sleep", lambda s: None)
    code, out, err = run(capsys, "--apply")
    assert code == 1
    assert source.read_text(encoding="utf-8") == 'x = "#save"\n'
    assert "mendwright fix: a.py was not changed: Permission denied. Close the file in other " \
           "programs, or edit it by hand, then run again." in err
    assert [p.name for p in root.iterdir() if p.name.startswith(".mendwright-fix")] == []
    assert out.splitlines()[0] == ("mendwright fix: wrote 0 files of 1. The errors are listed "
                                   "after the changes.")


def test_a_file_that_changed_since_it_was_read_is_not_overwritten(root):
    plan = fixback.plan_fixes(
        {"selectors": {"k": healed("#save", "//b[@id='y']")}}, [canary("k", "//b[@id='y']")])
    source = write(root, "a.py", 'x = "#save"\n')
    result = fixback.build_result(plan, [str(root)])
    assert len(result.changes) == 1
    source.write_text('x = "#save"  # edited meanwhile\n', encoding="utf-8")
    fixback.apply_result(result)
    assert source.read_text(encoding="utf-8") == 'x = "#save"  # edited meanwhile\n'
    assert result.errors == ["a.py was not changed: it changed while mendwright fix was running. "
                             "Close the file in other programs, or edit it by hand, then run again."]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_file_mode_is_kept(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    source = write(root, "a.py", 'x = "#save"\n')
    os.chmod(source, 0o750)
    assert run(capsys, "--apply")[0] == 0
    assert stat.S_IMODE(os.stat(source).st_mode) == 0o750


def test_the_text_and_the_diff_never_carry_carriage_returns(root, capsys):
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    write(root, "a.py", 'a = 1\nb = "#save"\nc = 2\n', newline="\r\n")
    out = run(capsys)[1]
    assert "\r" not in out
    assert '-b = "#save"' in out.splitlines()


# --- the command line --------------------------------------------------------------------

def test_fix_help_lists_the_options(capsys):
    assert cli.main(["fix", "--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: mendwright fix")
    for option in ("--store", "--path", "--glob", "--include-unconfirmed", "--apply"):
        assert option in out


def test_fix_is_listed_in_the_main_help(capsys):
    assert cli.main(["--help"]) == 0
    assert "fix" in capsys.readouterr().out


def test_fix_does_not_call_a_model_or_import_a_browser(root, capsys, monkeypatch):
    for name in cli.PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)
    confirmed_project(root, "k", "#save", "//b[@id='y']")
    write(root, "a.py", 'x = "#save"\n')
    before = {m for m in ("playwright", "selenium") if m in sys.modules}
    assert run(capsys)[0] == 0
    assert {m for m in ("playwright", "selenium") if m in sys.modules} == before


def test_the_plan_names_what_it_will_do_without_touching_files():
    store = {"selectors": {
        "a": healed("#a", "//a[@id='n']", role="link"),
        "b": {"expectedRole": "button", "candidates": [cand("#b", "code", T1)]},
        "c": healed("#c", "//c[@id='n']", destructive=True),
    }}
    plan = fixback.plan_fixes(store, [canary("a", "//a[@id='n']"), canary("c", "//c[@id='n']")])
    assert [(f.key, f.new, f.old, f.confirmed, f.role) for f in plan.fixes] == [
        ("a", "//a[@id='n']", ["#a"], True, "link")]
    assert [k for k, _ in plan.skipped] == ["c"]
    assert plan.new_for == {"#a": "//a[@id='n']"}
    assert plan.ambiguous == {}


# --- with a real heal --------------------------------------------------------------------

def test_a_real_heal_is_written_back_and_then_wins_on_the_next_run(root, capsys, make_page,
                                                                  stub_provider):
    """The store and journal come from mendwright itself: heal, confirm, fix, run again."""
    import mendwright

    old, healed_xpath = "//button[@id='a']", "//button[text()='C']"
    page_html = "<form><button id='a'>A</button><button id='c'>C</button></form>"
    store_path = str(root / ".mendwright" / "overrides.json")
    source = write(root, "tests/test_shop.py",
                   'def test_go(heal):\n    heal.locator("go", "' + old + '", role="button").click()\n')

    def run_the_test(html, selector, proposal=None):
        provider = stub_provider(proposal)
        healer = mendwright.open(make_page(html), provider=provider, store_path=store_path)
        proxy = healer.locator("go", selector, role="button", timeout_ms=0, poll_ms=0)
        proxy.click(expect=lambda: True)          # a check of the test's own confirms the heal
        return healer, provider

    run_the_test(page_html, old)                                       # the selector works
    broken = page_html.replace("id='a'", "id='a2'")
    _, provider = run_the_test(broken, old, healed_xpath)              # redesign: healed, click ok
    assert len(provider.bundles) == 1

    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    assert "go  (button)  healed, confirmed" in out
    text = source.read_text(encoding="utf-8")
    assert text == ('def test_go(heal):\n    heal.locator("go", "' + healed_xpath
                    + '", role="button").click()\n')
    ast.parse(text)

    healer, provider = run_the_test(broken, healed_xpath)              # the next run, code fixed
    assert provider.bundles == []
    assert [c["xpath"] for c in healer.store.candidates("go")] == [healed_xpath]
    saved = json.loads((root / ".mendwright" / "overrides.json").read_text(encoding="utf-8"))
    by_xpath = {c["xpath"]: c for c in saved["selectors"]["go"]["candidates"]}
    assert by_xpath[healed_xpath]["source"] == "code"
    assert by_xpath[old]["status"] == "superseded"

    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "mendwright fix: nothing to write back."


# --- heal.page.get_by_*() calls: never rewritten -------------------------------------------

SIGN_IN = 'get_by_role("button", name="Sign in")'
LOGIN = "//button[@id='login']"
SIGN_IN_SOURCE = (
    'import re\n'
    '\n'
    'def test_login(page):\n'
    '    page.get_by_role("button", name="Sign in").click()\n'
    "    page.get_by_role(role='button', name='Sign in', exact=False).click()\n"
    '    NAME = "Sign in"\n'
    '    page.get_by_role("button", name=NAME).click()\n'
    '    page.get_by_role("button", name=re.compile("sign in", re.I)).click()\n'
    '    expect_values = {\'get_by_role("button", name="Sign in")\': "money"}\n')


def _call_line(state, where):
    return ('  ' + SIGN_IN + ': it is a heal.page.get_by_role() call, and mendwright fix does not '
            'rewrite calls: from the code it cannot tell whether a call with these arguments runs '
            'on heal.page or inside another locator, where it may still find the element it was '
            'written for. Its healed selector, ' + state + ', is ' + LOGIN + ". Change the call's "
            "arguments by hand to the element's new role or words, or replace it with "
            'page.locator("' + LOGIN + '"). ' + where)


def test_a_call_key_is_left_alone_with_its_healed_selector_and_places(root, capsys):
    confirmed_project(root, SIGN_IN, SIGN_IN, LOGIN, role="button")
    source = write(root, "tests/test_login.py", SIGN_IN_SOURCE)
    before = source.read_bytes()
    code, out, err = run(capsys, "--apply")
    assert (code, err) == (0, "")
    assert source.read_bytes() == before        # the dict key equal to the call text too
    lines = out.splitlines()
    assert lines[0] == "mendwright fix: nothing to write back."
    assert "Left alone:" in lines
    assert _call_line('confirmed', 'Calls with these arguments: tests/test_login.py:4, '
                                   'tests/test_login.py:5.') in lines


def test_an_unconfirmed_call_heal_and_no_place_found(root, capsys):
    write_store(root, {SIGN_IN: healed(SIGN_IN, LOGIN, role="button")}, [])
    write(root, "test_a.py", 'NAME = "Sign in"\npage.get_by_role("button", name=NAME).click()\n')
    write(root, "notes.txt", 'page.get_by_role("button", name="Sign in")\n')
    code, out, err = run(capsys)
    assert (code, err) == (0, "")
    assert _call_line('not confirmed yet (check that it finds the right element)',
                      'No call with these arguments written as literals was found in 1 Python '
                      'file; calls whose arguments come from variables are not searched.') in (
        out.splitlines())


def test_more_than_ten_places_are_cut(root, capsys):
    confirmed_project(root, SIGN_IN, SIGN_IN, LOGIN, role="button")
    write(root, "test_many.py", 'page.get_by_role("button", name="Sign in")\n' * 13)
    code, out, _err = run(capsys)
    places = ', '.join('test_many.py:' + str(n) for n in range(1, 11))
    assert _call_line('confirmed', 'Calls with these arguments: ' + places + ', and 3 more.') in (
        out.splitlines())


def test_a_destructive_call_key_keeps_the_destructive_reason(root, capsys):
    confirmed_project(root, SIGN_IN, SIGN_IN, LOGIN, role="button", destructive=True)
    code, out, _err = run(capsys)
    assert ("  " + SIGN_IN + ": it is marked destructive, so its heals are never written into "
            "code. Fix the selector by hand.") in out.splitlines()
    assert "is a heal.page.get_by_role() call" not in out


def test_python_call_sites_reads_literal_calls_only():
    text = ('page.get_by_role("button", name="Sign in")\n'
            'x.get_by_role(role="button", name="Sign in")\n'
            'page.get_by_role("button", name=name)\n'
            'page.get_by_text(re.compile("total", re.I))\n'
            'get_by_role("button", name="Sign in")\n'
            'page.get_by_role("button", name="Sign in", checked=True)\n')
    keys = {SIGN_IN, 'get_by_text(re.compile("total", re.IGNORECASE))'}
    assert fixback.python_call_sites(text, keys) == {
        SIGN_IN: [1, 2], 'get_by_text(re.compile("total", re.IGNORECASE))': [4]}
    assert fixback.python_call_sites('def broken(:\n', keys) == {}


def test_a_call_key_never_enters_the_table_of_strings_to_replace():
    store = {"schema": 2, "selectors": {SIGN_IN: healed(SIGN_IN, LOGIN, role="button")}}
    plan = fixback.plan_fixes(store, [canary(SIGN_IN, LOGIN)])
    assert plan.fixes == [] and plan.new_for == {}
    assert plan.calls == [fixback.CallLeft(SIGN_IN, 'get_by_role', LOGIN, True)]


# --- list heals that heal.locator_all(..., nonempty=True) proposed ---------------------------------

ROWS = "//div[contains(concat(' ', normalize-space(@class), ' '), ' cart-row ')]"
T2 = "2026-10-03T10:00:00+00:00"


def list_row(xpath, status, pid="3f2a9c1d", at=T1, accepted=None, css="div.cart-row",
             differs=(), was=(".cart-item",)):
    row = dict(cand(xpath, "heal", None, status=status), list=True, css=css, proposal={
        "id": pid, "at": at, "by": "fingerprint", "page": "/cart", "count": 3, "found": 3,
        "recorded": 3, "score": 0.86, "next": 0.31,
        "was": {"selectors": list(was), "count": 3, "label": "Your bag",
                "container": '<section class="cart">', "items": ["Blue mug"]},
        "now": {"label": "Shopping cart" if differs else "Your bag",
                "container": '<section class="cart">', "alike": [1, 1], "items": ["Blue mug"]},
        "differs": list(differs)})
    if accepted:
        row["acceptedAt"] = accepted
    return row


def cart_key(*rows, code=(".cart-item",), **extra):
    entry = {"expectedRole": "div", "listFingerprint": {"v": 1, "count": 3, "items": []},
             "candidates": [cand(c, "code", T0) for c in code] + list(rows)}
    entry.update(extra)
    return entry


CART_TEST = 'for row in heal.locator_all("cart", ".cart-item", nonempty=True):\n    row.click()\n'


@pytest.mark.parametrize("flag", [(), ("--include-unconfirmed",)])
def test_a_proposed_list_heal_is_left_alone_with_the_command_that_accepts_it(root, capsys, flag):
    write_store(root, {"cart": cart_key(list_row(ROWS, "proposed"))})
    source = write(root, "test_cart.py", CART_TEST)
    code, out, _err = run(capsys, "--apply", *flag)
    assert code == 0 and source.read_text(encoding="utf-8") == CART_TEST
    assert ("  cart: its list heal waits for you to check it. Run mendwright lists, then: "
            "mendwright lists accept cart 3f2a9c1d") in out.splitlines()


def test_an_accepted_list_heal_is_written_into_the_list_call(root, capsys):
    write_store(root, {"cart": cart_key(list_row(ROWS, "accepted", accepted=T2,
                                                 differs=("heading",)))})
    source = write(root, "test_cart.py", CART_TEST)
    code, out, _err = run(capsys, "--apply")
    assert code == 0
    assert source.read_text(encoding="utf-8") == CART_TEST.replace('".cart-item", nonempty',
                                                                   '"div.cart-row", nonempty')
    lines = out.splitlines()
    assert "cart  (div)  list heal, accepted 2026-10-03 10:00:00 UTC" in lines
    assert "  was  .cart-item" in lines and "  now  div.cart-row" in lines
    assert "       3 elements under 'Shopping cart', in <section class=\"cart\">" in lines
    assert ("  check  The heading is not the same: 'Your bag' then, 'Shopping cart' now."
            in lines)


def test_an_accepted_list_heal_is_written_as_an_xpath_into_an_xpath(root, capsys):
    old = "//div[@class='cart-item']"
    write_store(root, {"cart": cart_key(list_row(ROWS, "accepted", accepted=T2, was=(old,)),
                                        code=(old,))})
    text = 'rows = heal.locator_all("cart", "//div[@class=\'cart-item\']", nonempty=True)\n'
    source = write(root, "test_cart.py", text)
    assert run(capsys, "--apply")[0] == 0
    assert ROWS in source.read_text(encoding="utf-8") and '"cart"' in source.read_text("utf-8")


def test_an_accepted_list_heal_with_a_newer_proposal_is_left_alone(root, capsys):
    write_store(root, {"cart": cart_key(
        list_row(ROWS, "accepted", accepted=T1),
        list_row("//li[@class='line']", "proposed", pid="9b8c7d6e", at=T2))})
    source = write(root, "test_cart.py", CART_TEST)
    code, out, _err = run(capsys, "--apply")
    assert code == 0 and source.read_text(encoding="utf-8") == CART_TEST
    assert ("  cart: a newer list heal waits for you to check it, so mendwright can't tell which "
            "one you want. Run mendwright lists, then accept or reject it.") in out.splitlines()


def test_what_keeps_a_heal_out_of_the_code_keeps_an_accepted_list_heal_out(root, capsys):
    accepted = list_row(ROWS, "accepted", accepted=T2)
    write_store(root, {
        "pay": cart_key(accepted, destructive=True),
        "seeded": cart_key(accepted, code=()),
        "word": cart_key(list_row(ROWS, "accepted", accepted=T2, was=("div",)), code=("div",)),
        "both": cart_key(accepted, cand("//div[@id='p1']", "heal", T1)),
        "framed": cart_key(dict(accepted, frameXPath="//iframe[@id='shop']"))})
    write(root, "test_cart.py", CART_TEST)
    code, out, _err = run(capsys, "--apply")
    lines = out.splitlines()
    assert code == 0 and out.splitlines()[0] == "mendwright fix: nothing to write back."
    assert ("  pay: it is marked destructive, so its heals are never written into code. Fix the "
            "selector by hand.") in lines
    assert any(line.startswith("  seeded: no selector from your code is saved for it")
               and line.endswith("Update the seed file by hand, to: " + ROWS) for line in lines)
    assert any(line.startswith("  word: its old selector 'div' is a bare word") for line in lines)
    assert ("  framed: its healed selector runs inside the iframe //iframe[@id='shop'], and a "
            "selector in your code runs in the page itself. Change the selector and the frame "
            "handling by hand.") in lines
    assert ("  both: it has a heal of the list (heal.locator_all()) and a heal of one element "
            "under the same key, and one selector in your code cannot be both, so mendwright "
            "cannot tell which one goes where. Change the selectors by hand, or give the list a "
            "key of its own: the list to div.cart-row, the element to //div[@id='p1']") in lines


T3 = "2026-10-04T11:00:00+00:00"
WISH_TEST = ('def test_cart(heal):\n    rows = heal.locator_all("cart", ".cart-item", nonempty=True)\n'
             'def test_wish(heal):\n    rows = heal.locator_all("cart", ".wish")\n')


def test_an_accepted_list_heal_is_written_only_over_the_selector_it_replaced(root, capsys):
    # The key is used on two pages, and the wish list's call ran last: .cart-item is not one of
    # the last run's selectors, and .wish works. Nothing is written over .wish.
    write_store(root, {"cart": cart_key(
        cand(".cart-item", "code", T0, status="superseded"), cand(".wish", "code", T3),
        list_row(ROWS, "accepted", accepted=T2), code=())})
    source = write(root, "test_cart.py", WISH_TEST)
    code, out, _err = run(capsys, "--apply")
    assert code == 0 and source.read_text(encoding="utf-8") == WISH_TEST
    assert ("  cart: its list heal replaced '.cart-item', and the last run passed '.wish' for it "
            "instead: your code changed, or the key is used with another selector on another "
            "page, so mendwright can't tell that a string it would replace is the selector that "
            "broke. If '.cart-item' is still in your code, change it by hand to: div.cart-row. "
            "If your code already has a selector that works, drop the list heal with: mendwright "
            "lists reject cart") in out.splitlines()
    # The cart's call ran last: only that call is changed.
    write_store(root, {"cart": cart_key(
        cand(".wish", "code", T3, status="superseded"), list_row(ROWS, "accepted", accepted=T2))})
    assert run(capsys, "--apply")[0] == 0
    assert source.read_text(encoding="utf-8") == WISH_TEST.replace(
        '".cart-item", nonempty', '"div.cart-row", nonempty')


def test_an_accepted_list_heal_is_not_written_over_a_selector_fixed_by_hand(root, capsys):
    text = 'rows = heal.locator_all("cart", ".cart-row", nonempty=True)\n'
    write_store(root, {"cart": cart_key(
        cand(".cart-item", "code", T0, status="superseded"), cand(".cart-row", "code", T3),
        list_row(ROWS, "accepted", accepted=T2), code=())})
    source = write(root, "test_cart.py", text)
    code, out, _err = run(capsys, "--apply")
    assert code == 0 and source.read_text(encoding="utf-8") == text
    assert any(line.startswith("  cart: its list heal replaced '.cart-item', and the last run "
                               "passed '.cart-row' for it instead") for line in out.splitlines())


def test_an_accepted_list_heal_is_not_written_once_the_old_selector_matched_again(root, capsys):
    # A run that did not retire it (mendwright 0.8.0 on the same store) saw .cart-item match.
    write_store(root, {"cart": cart_key(list_row(ROWS, "accepted", accepted=T2),
                                        code=())})
    store = root / ".mendwright" / "overrides.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    data["selectors"]["cart"]["candidates"].insert(0, cand(".cart-item", "code", T3))
    store.write_text(json.dumps(data), encoding="utf-8")
    source = write(root, "test_cart.py", CART_TEST)
    code, out, _err = run(capsys, "--apply")
    assert code == 0 and source.read_text(encoding="utf-8") == CART_TEST
    assert ("  cart: its selector '.cart-item' matched on 2026-10-04 11:00:00 UTC, after its list "
            "heal was found on 2026-10-02 09:30:00 UTC, so the list was not broken there, or your "
            "code has a new selector already. Look at the page, then change the selector by hand "
            "to: div.cart-row, or drop the list heal with: mendwright lists reject cart"
            ) in out.splitlines()


def test_an_accepted_list_heal_without_its_evidence_counts_every_code_selector(root, capsys):
    # A 0.8.0 write dropped the proposal and acceptedAt: the row is still accepted.
    bare = {k: v for k, v in list_row(ROWS, "accepted").items() if k != "proposal"}
    write_store(root, {"cart": cart_key(bare)})
    source = write(root, "test_cart.py", CART_TEST)
    assert run(capsys, "--apply")[0] == 0
    assert '"div.cart-row", nonempty' in source.read_text(encoding="utf-8")
    write_store(root, {"cart": cart_key(cand(".wish", "code", T3), bare, code=())})
    text = 'rows = heal.locator_all("cart", ".wish")\n'
    wish = write(root, "test_wish.py", text)
    code, out, _err = run(capsys, "--apply")
    assert code == 0 and wish.read_text(encoding="utf-8") == text
    assert any(line.startswith("  cart: its selector '.wish' matched on 2026-10-04 11:00:00 UTC, "
                               "after its list heal was found on 2026-10-01 08:00:00 UTC")
               for line in out.splitlines())


def test_an_accepted_list_heal_is_not_written_once_a_seed_selector_matched_again(root, capsys):
    # The code's .cart-item last matched before the list heal was found, the seed file's
    # selector of the key after: the list was not broken there.
    seeded = "//div[@class='cart-item']"
    write_store(root, {"cart": cart_key(cand(seeded, "seed", T3),
                                        list_row(ROWS, "accepted", accepted=T2))})
    source = write(root, "test_cart.py", CART_TEST)
    code, out, _err = run(capsys, "--apply")
    assert code == 0 and source.read_text(encoding="utf-8") == CART_TEST
    assert any(line.startswith("  cart: its selector " + repr(seeded) + " matched on 2026-10-04 "
                               "11:00:00 UTC, after its list heal was found on 2026-10-02 "
                               "09:30:00 UTC") for line in out.splitlines())


def test_an_accepted_list_heal_that_names_no_selector_it_replaced_is_left_alone(root, capsys):
    write_store(root, {"cart": cart_key(list_row(ROWS, "accepted", accepted=T2, was=()))})
    source = write(root, "test_cart.py", CART_TEST)
    code, out, _err = run(capsys, "--apply")
    assert code == 0 and source.read_text(encoding="utf-8") == CART_TEST
    assert ("  cart: its list heal doesn't name the selector it replaced, so mendwright can't "
            "tell which string in your code to replace. Change it by hand to: div.cart-row. If "
            "your code already has a selector that works, drop the list heal with: mendwright "
            "lists reject cart") in out.splitlines()
