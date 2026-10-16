"""`mendwright fix --format json` and `--format md`, and `--out`: the machine output the
GitHub Action reads, and the Markdown it puts in a pull request.

Each test builds a small project in tmp_path, as tests/test_fix.py does, and runs
mendwright.cli.main([...]) in-process. Offline: no network, no model, no browser.
"""

import json
import re
import textwrap

import pytest

from mendwright import cli, fixback

T0 = "2026-10-01T08:00:00+00:00"
T1 = "2026-10-02T09:30:00+00:00"
T2 = "2026-10-03T10:00:00+00:00"
TERMS = "//input[@name='legal-consent']"
TERMS_CSS = 'input[name="legal-consent"]'


def cand(xpath, source, last=None, status="active", **extra):
    c = {"xpath": xpath, "source": source, "status": status, "addedAt": T0, "lastSucceededAt": last}
    c.update(extra)
    return c


def healed(old, new, role="checkbox", css=None, **extra):
    olds = [old] if isinstance(old, str) else list(old)
    heal = cand(new, "heal", T1, **({"css": css} if css else {}))
    entry = {"expectedRole": role, "candidates": [cand(o, "code", T0) for o in olds] + [heal]}
    entry.update(extra)
    return entry


def applied(key, new, provider="StubProvider", model="stub-model", ts=T1, **extra):
    event = {"ts": ts, "event": "heal.applied", "key": key, "provider": provider, "model": model,
             "latency_ms": 12, "xpath": new, "old": []}
    event.update(extra)
    return {k: v for k, v in event.items() if v is not None}


def canary(key, xpath, success=True, ts=T1, **extra):
    event = {"ts": ts, "event": "heal.canary", "key": key, "xpath": xpath, "success": success}
    event.update(extra)
    return event


def write_store(root, selectors, events=()):
    folder = root / ".mendwright"
    folder.mkdir(parents=True, exist_ok=True)
    store = folder / "overrides.json"
    store.write_text(json.dumps({"schema": 2, "selectors": selectors}), encoding="utf-8")
    if events is not None:
        (folder / "journal.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return store


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def run(capsys, *args):
    code = cli.main(["fix", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def fix_json(capsys, *args):
    code, out, err = run(capsys, "--format", "json", *args)
    assert out.endswith("\n") and out.isascii()
    return code, json.loads(out), err


@pytest.fixture
def root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def checkout_project(root):
    """The spec's example: accept_terms healed by the fingerprint and confirmed by a test,
    search healed by a model and confirmed by expect=, email never confirmed."""
    store = write_store(root, {
        "accept_terms": healed("#accept-terms", TERMS, css=TERMS_CSS),
        "search": healed("#search-box", "//input[@name='q']", role="textbox"),
        "email": healed("#email", "//input[@name='e-mail']", role="textbox"),
    }, [
        applied("accept_terms", TERMS, provider="fingerprint", model=None, source="fingerprint"),
        canary("accept_terms", TERMS, by="test", test="tests/test_checkout.py::test_signup", ts=T2),
        applied("search", "//input[@name='q']", provider="AnthropicProvider", model="claude-x"),
        canary("search", "//input[@name='q']", by="expect"),
        applied("email", "//input[@name='e-mail']"),
    ])
    source = write(root, "tests/test_checkout.py", textwrap.dedent('''\
        def test_signup(heal):
            heal.locator("email", "#email").fill("ada@example.com")
            heal.locator("accept_terms", "#accept-terms", role="checkbox").check()

        def test_search(heal):
            heal.locator("search", "#search-box").fill("x")
            heal.locator("search", "#search-box").press("Enter")
    '''))
    return store, source


# --- JSON ----------------------------------------------------------------------------------

def test_json_lists_what_is_written_back_and_where(root, capsys):
    store, source = checkout_project(root)
    before = source.read_bytes()
    code, data, err = fix_json(capsys)
    assert (code, err) == (0, "")
    assert source.read_bytes() == before
    assert data["schema"] == 1
    assert data["mendwright"] == __import__("mendwright").__version__
    assert data["store"] == str(store)
    assert data["journal"] == {"path": str(store.parent / "journal.jsonl"), "exists": True}
    assert (data["applied"], data["scanned"], data["errors"]) == (False, 1, [])
    assert re.fullmatch(r"[0-9a-f]{64}", data["plan"])
    terms, search = data["written_back"]
    assert terms == {
        "key": "accept_terms", "role": "checkbox", "old": ["#accept-terms"], "now": [TERMS_CSS],
        "xpath": TERMS, "css": TERMS_CSS, "confirmed": True,
        "found_by": {"tier": "fingerprint", "provider": "fingerprint", "model": None, "at": T1},
        "confirmed_by": {"by": "test", "test": "tests/test_checkout.py::test_signup", "at": T2},
        "places": [{"path": "tests/test_checkout.py", "line": 3}]}
    assert search["found_by"] == {"tier": "model", "provider": "AnthropicProvider",
                                  "model": "claude-x", "at": T1}
    assert search["confirmed_by"] == {"by": "expect", "test": None, "at": T1}
    assert search["places"] == [{"path": "tests/test_checkout.py", "line": 6},
                                {"path": "tests/test_checkout.py", "line": 7}]
    assert data["replacements"][0] == {
        "path": "tests/test_checkout.py", "line": 3, "keys": ["accept_terms"],
        "selector": "#accept-terms", "new": TERMS_CSS, "was": '"#accept-terms"',
        "now": "'" + TERMS_CSS + "'"}
    assert [r["line"] for r in data["replacements"]] == [3, 6, 7]
    assert data["files"] == [{"path": "tests/test_checkout.py", "replacements": 3,
                              "written": False}]
    assert [(i["code"], i["key"]) for i in data["left_alone"]] == [("unconfirmed", "email")]


def test_json_with_apply_says_which_files_were_written(root, capsys):
    checkout_project(root)
    code, data, err = fix_json(capsys, "--apply")
    assert (code, err) == (0, "")
    assert data["applied"] is True
    assert data["files"][0]["written"] is True
    code, data, err = fix_json(capsys, "--apply")
    assert data["written_back"] == [] and data["files"] == []
    assert data["plan"] == fixback.plan_hash([])
    assert {i["code"] for i in data["left_alone"]} == {"already_in_code", "unconfirmed"}


def test_found_by_names_the_tier_provider_and_model(root, capsys):
    keys = {"fp": dict(provider="fingerprint", model=None, source="fingerprint"),
            "model": dict(provider="OpenAICompatibleProvider", model="gpt-x"),
            "nameless": dict(provider=None, model=None),
            "intent": dict(provider="AnthropicProvider", model="claude-x", source="intent"),
            "none": None}
    selectors, events, text = {}, [], ""
    for key, how in keys.items():
        new = "//button[@id='" + key + "-new']"
        selectors[key] = healed("#" + key, new, role="button")
        if how is not None:
            events.append(applied(key, new, **how))
        events.append(canary(key, new))
        text += 'heal.locator("' + key + '", "#' + key + '")\n'
    write_store(root, selectors, events)
    write(root, "test_a.py", text)
    found = {w["key"]: w["found_by"] for w in fix_json(capsys)[1]["written_back"]}
    assert found["fp"]["tier"] == "fingerprint"
    assert found["model"] == {"tier": "model", "provider": "OpenAICompatibleProvider",
                              "model": "gpt-x", "at": T1}
    assert found["nameless"] == {"tier": "model", "provider": None, "model": None, "at": T1}
    assert found["intent"]["tier"] == "intent"
    assert found["none"] is None


def test_confirmed_by_reads_the_canary_that_decides(root, capsys):
    new = "//button[@id='new']"
    write_store(root, {"old": healed("#old", new, role="button"),
                       "late": healed("#late", "//a[@id='late']", role="link"),
                       "never": healed("#never", "//a[@id='never']", role="link")},
                [canary("old", new),                             # a canary from 0.8.0
                 canary("late", "//a[@id='late']", by="test", test="t::a"),
                 canary("late", "//a[@id='late']", False, by="action"),
                 canary("late", "//a[@id='late']", by="confirm", ts=T2)])
    write(root, "test_a.py", 'heal.locator("old", "#old")\nheal.locator("late", "#late")\n'
                             'heal.locator("never", "#never")\n')
    data = fix_json(capsys, "--include-unconfirmed")[1]
    confirmed = {w["key"]: w["confirmed_by"] for w in data["written_back"]}
    assert confirmed == {"late": {"by": "confirm", "test": None, "at": T2},
                         "never": None,
                         "old": {"by": None, "test": None, "at": T1}}


def test_the_plan_hash_ignores_lines_and_quotes_and_not_what_is_written(root, capsys):
    write_store(root, {"k": healed("#a", "//b[@id='y']", role="button"),
                       "j": healed("#j", "//b[@id='j']", role="button")},
                [canary("k", "//b[@id='y']"), canary("j", "//b[@id='j']")])
    source = write(root, "test_a.py", 'x = "#a"\n')
    first = fix_json(capsys)[1]["plan"]
    write(root, "test_a.py", "# a new line\n\ny = 1\nx = '#a'\n")
    assert fix_json(capsys)[1]["plan"] == first
    write(root, "test_a.py", 'x = "#a"\nz = "#a"\n')
    assert fix_json(capsys)[1]["plan"] != first
    write(root, "test_a.py", 'x = "#j"\n')
    assert fix_json(capsys)[1]["plan"] != first
    source.write_text('x = "#nothing"\n', encoding="utf-8")
    assert fix_json(capsys)[1]["plan"] == fixback.plan_hash([])


def test_plan_hash_is_over_path_selector_new_and_keys():
    a = {"path": "t.py", "selector": "#a", "new": "//a", "keys": ["k"], "line": 1, "was": "", "now": ""}
    b = dict(a, line=9, was="'#a'", now='"//a"')
    assert fixback.plan_hash([a]) == fixback.plan_hash([b])
    assert fixback.plan_hash([a]) != fixback.plan_hash([dict(a, keys=["k", "j"])])
    assert fixback.plan_hash([a]) != fixback.plan_hash([a, a])
    assert fixback.plan_hash([]) == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


# Every code of left_alone, each from a project of its own: (code, store, events, files,
# extra arguments).
NEW = "//button[@id='new']"


def _one(key="k", old="#a", new=NEW, role="button", **extra):
    return {key: healed(old, new, role=role, **extra)}


CODE_CASES = {
    "destructive": (_one(destructive=True), [canary("k", NEW)], {"a.py": 'x = "#a"\n'}, []),
    "iframe": ({"k": {"expectedRole": "button", "candidates": [
        cand("#a", "code", T0), cand(NEW, "heal", T1, frameXPath="//iframe")]}},
        [canary("k", NEW)], {"a.py": 'x = "#a"\n'}, []),
    "not_a_selector": (_one(new="#b"), [canary("k", "#b")], {"a.py": 'x = "#a"\n'}, []),
    "seed_only": ({"k": {"expectedRole": "button", "candidates": [
        cand("#a", "seed", T0), cand(NEW, "heal", T1)]}}, [canary("k", NEW)],
        {"a.py": 'x = "#a"\n'}, []),
    "list_and_element": ({"k": {"expectedRole": "div", "candidates": [
        cand(".c", "code", T0), cand("//div[@id='l']", "heal", T1, list=True),
        cand("//div[@id='p']", "heal", T1)]}}, [], {"a.py": 'x = ".c"\n'}, []),
    "list_heal": ({"k": {"expectedRole": "div", "candidates": [
        cand(".c", "code", T0), cand("//div[@id='l']", "heal", T1, list=True)]}}, [],
        {"a.py": 'x = ".c"\n'}, []),
    "list_key": ({"k": dict(healed(".c", "//div[@id='p']", role="div"),
                            listFingerprint={"version": 1})}, [], {"a.py": 'x = ".c"\n'}, []),
    "bare_word": (_one(old="button"), [canary("k", NEW)], {"a.py": 'x = "button"\n'}, []),
    "driver_by": (_one(key="id=login", old='[id="login"]'), [canary("id=login", NEW)],
                  {"a.py": 'x = \'[id="login"]\'\n'}, []),
    "unconfirmed": (_one(), [], {"a.py": 'x = "#a"\n'}, []),
    "quarantined": ({"k": {"expectedRole": "button", "candidates": [
        cand("#a", "code", "2026-09-30T00:00:00+00:00"), cand(NEW, "heal", None, status="quarantined")]}},
        [canary("k", NEW, False)], {"a.py": 'x = "#a"\n'}, []),
    "ambiguous": ({**_one(), **_one(key="j", new="//button[@id='j']")},
                  [canary("k", NEW), canary("j", "//button[@id='j']")],
                  {"a.py": 'x = "#a"\ny = "#a"\n'}, []),
    "not_python": (_one(), [canary("k", NEW)], {"a.py": 'x = "#a"\nif\n'}, []),
    "css_only": (_one(), [canary("k", NEW)],
                 {"a.py": 'driver.find_element(By.CSS_SELECTOR, "#a")\n'}, []),
    "no_escape": (_one(new="//button[@id=\"new\"]"), [canary("k", "//button[@id=\"new\"]")],
                  {"a.go": 'sel := "#a"\n'}, ["--glob", "*.go"]),
    "flow_selector": (_one(), [canary("k", NEW)], {"a.py": 'heal.interrupt("i", "#a")\n'}, []),
    "already_in_code": (_one(), [canary("k", NEW)], {"a.py": 'x = "' + NEW + '"\n'}, []),
    "not_found": (_one(), [canary("k", NEW)], {"a.py": 'x = "#elsewhere"\n'}, []),
}


@pytest.mark.parametrize("code", sorted(CODE_CASES))
def test_every_left_alone_code_with_the_text_of_the_text_output(root, capsys, code):
    selectors, events, files, args = CODE_CASES[code]
    write_store(root, selectors, events)
    for name, text in files.items():
        write(root, name, text)
    exit_code, text_out, _ = run(capsys, *args)
    exit_json, data, _ = fix_json(capsys, *args)
    assert exit_code == exit_json == 0
    lines = text_out.splitlines()
    shown = lines[lines.index("Left alone:") + 1:]
    shown = [line[2:] for line in shown[:shown.index("") if "" in shown else len(shown)]]
    assert [i["text"] for i in data["left_alone"]] == shown
    # A file that is not Python hides the selector, which is then found nowhere else.
    assert [i["code"] for i in data["left_alone"]] == (
        [code] + (["not_found"] if code == "not_python" else []))
    item = data["left_alone"][0]
    if code in ("ambiguous", "not_python", "css_only", "no_escape", "flow_selector"):
        assert item["key"] is None and item["path"] in ("a.py", "a.go")
        assert (item["line"] is None) == (code == "not_python")
    else:
        assert item["key"] in ("k", "id=login") and item["path"] is None and item["line"] is None
    assert code in fixback.CODES


def test_left_alone_keeps_the_order_of_the_text_output(root, capsys):
    write_store(root, {**_one(key="a1", destructive=True), **_one(key="b2", old="#b"),
                       **_one(key="c3", old="#c", new="//button[@id='c']")},
                [canary("a1", NEW), canary("c3", "//button[@id='c']")])
    write(root, "a.py", 'heal.interrupt("i", "#c")\nx = "#c"\n')
    write(root, "b.py", 'x = "#c"\nif\n')
    lines = run(capsys)[1].splitlines()
    data = fix_json(capsys)[1]
    shown = [line[2:] for line in lines[lines.index("Left alone:") + 1:] if line.startswith("  ")]
    assert [i["text"] for i in data["left_alone"]] == shown
    assert [i["code"] for i in data["left_alone"]] == ["destructive", "unconfirmed",
                                                         "flow_selector", "not_python"]


# --- --out and the exit codes ----------------------------------------------------------------

def test_out_writes_the_file_and_prints_only_where(root, capsys, tmp_path):
    checkout_project(root)
    target = tmp_path / "made" / "fix.json"
    code, out, err = run(capsys, "--format", "json", "--out", str(target))
    assert (code, err) == (0, "")
    assert out == "Wrote " + str(target) + "\n"
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == 1
    target = tmp_path / "fix.md"
    assert run(capsys, "--format", "md", "--out", str(target))[1] == "Wrote " + str(target) + "\n"
    assert target.read_text(encoding="utf-8").startswith("## mendwright fix\n")


def test_an_unwritable_out_exits_1_after_the_sources_are_written(root, capsys, tmp_path):
    _store, source = checkout_project(root)
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    code, out, err = run(capsys, "--apply", "--format", "json", "--out", str(blocker / "fix.json"))
    assert code == 1
    assert out == ""
    assert err.startswith("mendwright fix: could not write ")
    assert TERMS_CSS in source.read_text(encoding="utf-8")


def test_no_store_prints_nothing_on_stdout_and_writes_no_out_file(root, capsys, tmp_path):
    target = tmp_path / "fix.json"
    for fmt in ("json", "md"):
        code, out, err = run(capsys, "--format", fmt, "--out", str(target))
        assert code == 1 and out == "" and "no store at" in err
        assert not target.exists()
        code, out, err = run(capsys, "--format", fmt)
        assert code == 1 and out == ""


def test_errors_are_in_the_document_and_on_stderr(root, capsys, monkeypatch):
    checkout_project(root)

    def refuse(change):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(fixback, "_write_atomic", refuse)
    code, data, err = fix_json(capsys, "--apply")
    assert code == 1
    assert data["errors"] == ["tests/test_checkout.py was not changed: Permission denied. Close "
                              "the file in other programs, or edit it by hand, then run again."]
    assert data["files"][0]["written"] is False
    assert err == "mendwright fix: " + data["errors"][0] + "\n"


def test_the_text_output_is_unchanged_by_the_new_options(root, capsys):
    checkout_project(root)
    plain = run(capsys)
    assert run(capsys, "--format", "text") == plain
    assert plain[1].startswith("mendwright fix: 3 replacements in 1 file would put 2 healed "
                               "selectors into your code.")


def test_fix_help_lists_the_new_options(capsys):
    assert cli.main(["fix", "--help"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    for text in ("--format {text,json,md}", "--out FILE", "--destructive-key KEY [KEY ...]",
                 "output format: text (default), json for scripts, md for a job summary or pull "
                 "request", "write the output to FILE instead of stdout (the folder is created "
                 "when it is missing)"):
        assert text in out, text


# --- Markdown --------------------------------------------------------------------------------

def test_markdown_for_a_dry_run(root, capsys):
    checkout_project(root)
    code, out, err = run(capsys, "--format", "md")
    assert (code, err) == (0, "")
    assert out.splitlines()[:9] == [
        "## mendwright fix",
        "",
        "### Selectors to write back (2, dry run)",
        "",
        "| Key | Was | Now | Found by | Confirmed by | Where |",
        "| --- | --- | --- | --- | --- | --- |",
        "| `accept_terms` (checkbox) | `#accept-terms` | `" + TERMS_CSS + "` | fingerprint (no "
        "model) | passing test `tests/test_checkout.py::test_signup` | `tests/test_checkout.py:3` |",
        "| `search` (textbox) | `#search-box` | `//input[@name='q']` | model: "
        "`AnthropicProvider`, `claude-x` | `expect=` on an action | `tests/test_checkout.py:6`, "
        "`tests/test_checkout.py:7` |",
        "",
    ]
    assert out.splitlines()[9:16] == [
        "### Left alone (1)",
        "",
        "- `email`: its heal was never confirmed.",
        "  <details><summary>Why</summary>",
        "",
        "  ````text",
        "  email: its heal was never confirmed (journal.jsonl has no canary success for it; the "
        "pytest plugin or an expect= or confirm(True) check writes one in the run that makes the "
        "heal). Check that the selector finds the right element, then add --include-unconfirmed "
        "to write it anyway.",
    ]
    assert out.endswith("  ````\n\n  </details>\n")


def test_markdown_after_apply_and_with_nothing_to_do(root, capsys):
    checkout_project(root)
    out = run(capsys, "--format", "md", "--apply")[1]
    assert "### Selectors written back (2)" in out.splitlines()
    out = run(capsys, "--format", "md")[1]
    assert out.splitlines()[:3] == ["## mendwright fix", "", "Nothing to write back."]
    assert "### Left alone (3)" in out.splitlines()
    write_store(root, {})
    assert run(capsys, "--format", "md")[1] == "## mendwright fix\n\nNothing to write back.\n"


def _row(**item):
    base = {"key": "k", "role": "button", "old": ["#a"], "now": [NEW], "found_by": None,
            "confirmed_by": None, "places": [{"path": "t.py", "line": 1}]}
    base.update(item)
    text = fixback.render_markdown({"applied": True, "written_back": [base], "left_alone": []})
    return text.splitlines()[6]


def test_markdown_found_by_and_confirmed_by_columns():
    def cells(**item):
        return [c.strip() for c in _row(**item)[2:-2].split(" | ")]

    assert cells()[3:5] == ["unknown (no heal.applied in journal.jsonl)", "**not confirmed**"]
    model = {"tier": "model", "provider": "P", "model": None}
    assert cells(found_by=model)[3] == "model: `P`"
    assert cells(found_by=dict(model, provider=None))[3] == "model"
    assert cells(found_by={"tier": "intent"})[3] == "model, from intent="
    assert cells(found_by={"tier": "frame"})[3] == "inside an iframe"
    for by, shown in (("expect", "`expect=` on an action"),
                      ("expect_value", "`expect_value=` on a read"),
                      ("confirm", "`confirm(True)`"),
                      (None, "a check before mendwright 0.9.0, which did not record which"),
                      ("test", "passing test")):
        assert cells(confirmed_by={"by": by, "test": None})[4] == shown


def test_markdown_where_names_three_places_then_counts_and_takes_a_prefix():
    places = [{"path": "t.py", "line": n} for n in range(1, 6)]
    item = {"key": "k", "role": None, "old": ["#a", "#b"], "now": [NEW], "places": places}
    text = fixback.render_markdown({"applied": True, "written_back": [item], "left_alone": [
        {"code": "css_only", "key": None, "path": "u.py", "line": 4, "text": "u.py:4 was not ..."}]},
        path_prefix="services/web/")
    row = text.splitlines()[6]
    assert row.startswith("| `k` | `#a`, `#b` | `" + NEW + "` |")
    assert row.endswith("| `services/web/t.py:1`, `services/web/t.py:2`, `services/web/t.py:3` "
                        "and 2 more |")
    assert "- `services/web/u.py:4`: passes the selector where only CSS runs." in text.splitlines()


def test_markdown_heading_level():
    text = fixback.render_markdown({"written_back": [], "left_alone": [
        {"code": "unconfirmed", "key": "k", "text": "k: ..."}]}, heading="###")
    assert text.splitlines()[:5] == ["### mendwright fix", "", "Nothing to write back.", "",
                                     "#### Left alone (1)"]


# --- what Markdown makes of hostile text -------------------------------------------------------

HOSTILE = ["`", "``", "```", "`lead", "trail`", "a|b", "@octocat", "#1", "closes #1",
           "[x](https://e.example)", "![i](https://e.example/i.png)", "<script>alert(1)</script>",
           "<!-- -->", "-->", "line\nbreak", "esc\x1b[31m", "rtl‮evil", "iso⁦x",
           "lone\ud800", "", "**bold**", "<details>", "  spaced  "]
_PAYLOAD = re.compile(r'octocat|#1|e\.example|<script|<!--|-->|\*\*bold|\x1b|‮|⁦|\ud800')


def _outside_code(text):
    """What a CommonMark reader treats as markup: text with fenced blocks and code spans
    taken out. Fences open with three or more backticks and close with a run at least as
    long; a code span is a run of backticks closed by the next run of the same length."""
    out, lines, fence = [], text.split("\n"), None
    for line in lines:
        stripped = line.lstrip(" ")
        if fence is not None:
            m = re.match(r"(`{3,})\s*$", stripped)
            if m and len(m.group(1)) >= fence:
                fence = None
            continue
        m = re.match(r"(`{3,})[^`]*$", stripped)
        if m:
            fence = len(m.group(1))
            continue
        out.append(line)
    rest = "\n".join(out)
    kept, i = [], 0
    while i < len(rest):
        if rest[i] != "`":
            kept.append(rest[i])
            i += 1
            continue
        run = re.match(r"`+", rest[i:]).group()
        close = re.compile(r"(?<!`)" + run + r"(?!`)")
        m = close.search(rest, i + len(run))
        if m is None:
            kept.append(run)
            i += len(run)
        else:
            kept.append("<code>")
            i = m.end()
    return "".join(kept)


def test_store_text_lands_only_in_code_spans_and_fences():
    written = [{"key": k, "role": k, "old": [k], "now": [k],
                "found_by": {"tier": "model", "provider": k, "model": k},
                "confirmed_by": {"by": "test", "test": k},
                "places": [{"path": k, "line": 1}]} for k in HOSTILE]
    left = ([{"code": "unconfirmed", "key": k, "path": None, "line": None, "text": k + ": " + k}
             for k in HOSTILE]
            + [{"code": "css_only", "key": None, "path": k, "line": 2, "text": k} for k in HOSTILE])
    text = fixback.render_markdown({"applied": True, "written_back": written, "left_alone": left},
                                   path_prefix="")
    outside = _outside_code(text)
    assert not _PAYLOAD.search(outside), [m.group() for m in _PAYLOAD.finditer(outside)]
    assert "(empty)" in outside
    assert outside.count("<details>") == len(left)        # mendwright's own, one per item
    # Bidirectional controls and escapes are shown as text, not applied.
    assert "rtl\\u202Eevil" in text and "esc\\u001B[31m" in text and "lone\\uD800" in text
    assert "‮" not in text and "\x1b" not in text
    # Each table row still has six cells.
    for row in [line for line in text.splitlines() if line.startswith("| ") and "---" not in line][1:]:
        assert len(re.split(r"(?<!\\)\|", row)) == 8, row


def test_a_newline_in_a_key_cannot_end_the_details_block():
    text = fixback.render_markdown({"written_back": [], "left_alone": [
        {"code": "unconfirmed", "key": "k\n\n  </details>\n\n<img src=x>", "text":
         "k\n\n  </details>\n````\n<img src=x>"}]})
    assert "<img" not in _outside_code(text)


# --- size ------------------------------------------------------------------------------------

def _big(n):
    written = [{"key": "key_%04d" % i, "role": "button", "old": ["#old-%04d" % i],
                "now": ["//button[@id='new-%04d']" % i], "found_by": {"tier": "fingerprint"},
                "confirmed_by": {"by": "test", "test": "tests/test_x.py::test_%04d" % i},
                "places": [{"path": "tests/test_x.py", "line": i}]} for i in range(n)]
    left = [{"code": "unconfirmed", "key": "left_%04d" % i, "path": None, "line": None,
             "text": "left_%04d: its heal was never confirmed (journal.jsonl has no canary ...)" % i}
            for i in range(n)]
    return {"applied": True, "written_back": written, "left_alone": left}


def test_a_big_store_is_cut_details_first_then_items_then_rows():
    small = fixback.render_markdown(_big(3))
    assert "<details>" in small
    data = _big(3000)
    text = fixback.render_markdown(data)
    assert len(text) <= 54000
    assert "<details>" not in text
    assert "### Left alone (3000)" in text
    assert text.rstrip("\n").endswith(" more. Run mendwright fix with this store to see them "
                                      "all.")
    assert text.count("| `key_") < 3000
    m = re.search(r"…and (\d+) more\. Run mendwright fix", text)
    assert m and text.count("| `key_") + int(m.group(1)) == 3000
    for limit in (2000, 20000, 40000):
        assert len(fixback.render_markdown(data, limit=limit)) <= limit
    # Without details, items are cut before rows.
    data = _big(100)
    text = fixback.render_markdown(data, limit=18000)
    assert len(text) <= 18000
    assert text.count("| `key_") == 100 and 0 < text.count("- `left_") < 100
    m = re.search(r"…and (\d+) more\.", text)
    assert text.count("- `left_") + int(m.group(1)) == 100
