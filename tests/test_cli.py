"""The `mendwright` command: demo, doctor, report, --version and --help.

`mendwright fix` and `report --format html` have their own files: test_fix.py and
test_report_html.py.

Every test calls mendwright.cli.main([...]) in-process (or runs `python -m mendwright` in a
subprocess) and reads the output. Offline: no network, no model, no browser. The one
real-browser test is marked `browser` and is deselected by default.
"""

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import mendwright
from mendwright import cli
from mendwright.demo import (
    HEALED_XPATH,
    SCRIPTED_REPLY,
    ScriptedProvider,
    StandInPage,
    page_html,
    run,
    story,
)
from mendwright.demo import _story as story_module
from mendwright.prompt import build_prompt, parse_proposal

# The folder holding the mendwright package under test (the source tree, or site-packages
# when the tests run against an installed wheel). The subprocess tests import it from there.
PACKAGE_PARENT = Path(mendwright.__file__).resolve().parents[1]


def _child_env():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC_", "OPENAI_", "GEMINI_", "MENDWRIGHT_", "OLLAMA_"))}
    env["PYTHONPATH"] = str(PACKAGE_PARENT)
    return env


DEMO_LINES = [
    "mendwright demo: a redesign breaks two selectors on a checkout page and adds a cookie "
    "banner.",
    "Offline: a stand-in page (lxml, no browser) and a scripted model reply (no API call).",
    "",
    "  v1  accept_terms  #accept-terms   ok     matched, box checked",
    "  v1  pay           #pay            ok     matched (not clicked)",
    "      -- v2: a redesign ships, both ids are gone, and a cookie banner opens first --",
    "  v2  cookies       #cookie-accept  ok     banner covered the form, clicked it "
    "(heal.interrupt)",
    "  v2  accept_terms  #accept-terms   MISS   no element matches",
    "      model    proposed " + HEALED_XPATH + " (scripted reply, no API call)",
    "      checks   pass: 1 checkbox in the captured HTML, 1 match on the live page",
    "      retry    ok: box checked with the healed selector, heal confirmed",
    "  v2  pay           #pay            MISS   no element matches",
    "      refused  'pay' is marked destructive=True. Not healed, no model call.",
    "",
    "Done: 1 heal applied and confirmed, 1 refused, 1 interruption handled, 1 model call in "
    "total.",
]


@pytest.fixture(autouse=True)
def _no_provider_env(monkeypatch):
    """doctor reads provider variables; keep the developer's shell out of every test."""
    for name in cli.PROVIDER_VARS + ("NO_COLOR", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Point tempfile at a folder of our own, to see what the demo leaves behind."""
    import tempfile
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


# --- --version and --help ---------------------------------------------------------------

@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version(capsys, flag):
    assert cli.main([flag]) == 0
    assert capsys.readouterr().out == "mendwright " + mendwright.__version__ + "\n"


def test_help_lists_the_commands(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: mendwright ")
    for command in ("demo", "doctor", "report", "fix"):
        assert command in out


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage: mendwright " in capsys.readouterr().out


def test_unknown_command_is_a_usage_error(capsys):
    assert cli.main(["heal-everything"]) == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["demo", "doctor", "report", "fix"])
def test_each_command_has_help(capsys, command):
    assert cli.main([command, "--help"]) == 0
    assert capsys.readouterr().out.startswith("usage: mendwright " + command)


def test_python_dash_m_runs_main(tmp_path):
    # A subprocess, not runpy: under pytest the package can be loaded by the assertion
    # rewriting hook (mendwright registers a pytest plugin), which runpy cannot use.
    proc = subprocess.run([sys.executable, "-m", "mendwright", "--version"], cwd=tmp_path,
                          env=_child_env(), capture_output=True, text=True, timeout=60)
    assert (proc.returncode, proc.stdout, proc.stderr) == (
        0, "mendwright " + mendwright.__version__ + "\n", "")


def test_importing_main_module_does_not_run_anything(capsys):
    import importlib
    importlib.import_module("mendwright.__main__")
    assert capsys.readouterr().out == ""


# --- demo -------------------------------------------------------------------------------

def test_demo_output(capsys, temp_root):
    assert cli.main(["demo"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == DEMO_LINES
    assert captured.err == ""
    assert "\x1b[" not in captured.out  # not a terminal, so no colour


def test_demo_writes_nothing_into_the_cwd_and_cleans_up(tmp_path, monkeypatch, temp_root):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert cli.main(["demo"]) == 0
    assert list(cwd.iterdir()) == []
    assert list(temp_root.iterdir()) == []


def test_demo_keep_leaves_a_store_that_report_reads(capsys, temp_root):
    assert cli.main(["demo", "--keep"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:len(DEMO_LINES)] == DEMO_LINES
    folder = next(temp_root.iterdir())
    store = folder / "overrides.json"
    assert out[len(DEMO_LINES):] == [
        "Kept the store at " + str(store) + " (journal.jsonl is next to it).",
        'See it with: mendwright report --store "' + str(store) + '"',
    ]
    assert (folder / "journal.jsonl").exists()

    assert cli.main(["report", "--store", str(store), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    terms = next(k for k in report["keys"] if k["key"] == "accept_terms")
    sources = {c["selector"]: c["source"] for c in terms["candidates"]}
    assert sources == {"#accept-terms": "code", HEALED_XPATH: "heal"}
    pay = next(k for k in report["keys"] if k["key"] == "pay")
    assert pay["destructive"] is True                     # destructive=True was saved
    assert report["journal"]["counts"] == {"requested": 1, "applied": 1, "rejected": 0,
                                           "quarantined": 0}
    assert report["journal"]["last_applied"][0]["old"] == ["#accept-terms"]


def test_demo_colour_only_on_a_terminal_without_no_color(monkeypatch):
    monkeypatch.setattr(story_module, "_enable_windows_ansi", lambda: True)

    class Terminal:
        def __init__(self):
            self.text = ""

        def isatty(self):
            return True

        def write(self, text):
            self.text += text

        def flush(self):
            pass

    term = Terminal()
    assert run(stream=term) == 0
    assert "\x1b[32mok   \x1b[0m" in term.text
    assert "\x1b[31mMISS \x1b[0m" in term.text

    monkeypatch.setenv("NO_COLOR", "1")
    plain = Terminal()
    assert run(stream=plain) == 0
    assert "\x1b[" not in plain.text
    assert plain.text.splitlines() == DEMO_LINES


def test_demo_slow_pauses_between_lines(capsys):
    pauses = []
    assert run(slow=True, sleep=pauses.append) == 0
    lines = capsys.readouterr().out.splitlines()
    assert pauses == [0.4] * (len(lines) - 1)


def test_demo_ignores_mendwright_settings_from_the_environment(capsys, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere" / "store.json"
    monkeypatch.setenv("MENDWRIGHT_DISABLE", "1")
    monkeypatch.setenv("MENDWRIGHT_DRY_RUN", "1")
    monkeypatch.setenv("MENDWRIGHT_TIMEOUT_MS", "60000")
    monkeypatch.setenv("MENDWRIGHT_STORE", str(elsewhere))
    assert cli.main(["demo"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2] == "Note: MENDWRIGHT_DISABLE is set. The demo ignores it; your own runs do not."
    assert out[3:] == DEMO_LINES[2:]
    assert os.environ["MENDWRIGHT_DISABLE"] == "1"  # restored afterwards
    assert not elsewhere.parent.exists()


def test_demo_exits_1_when_the_heal_does_not_happen(capsys, monkeypatch, temp_root):
    # A reply the gate must reject (the button is not a checkbox): the CI step running
    # `mendwright demo` has to fail when healing breaks.
    monkeypatch.setattr(story_module, "SCRIPTED_REPLY", json.dumps({"xpath": "//button"}))
    assert cli.main(["demo"]) == 1
    captured = capsys.readouterr()
    assert "  v2  accept_terms  #accept-terms   MISS   not healed" in captured.out
    # The banner was still handled before the heal was tried.
    assert "  v2  cookies       #cookie-accept  ok" in captured.out
    assert "passed mendwright's local gate" in captured.out
    assert "this is a bug" in captured.err
    assert list(temp_root.iterdir()) == []


class LiveStub:
    """Stands in for the provider auto_provider() builds: it answers like a model would,
    and nothing is sent."""

    model = "stub-model"

    def __init__(self, xpath=HEALED_XPATH):
        self.xpath = xpath
        self.bundles = []
        self.last_error = None

    def propose(self, bundle):
        self.bundles.append(bundle)
        if self.xpath is None:
            self.last_error = "the stub has no answer. Try a stronger model."
            return None
        return {"xpath": self.xpath, "fallbacks": [], "reasoning": "stub"}

    def report_outcome(self, key, xpath, success):
        pass


def test_demo_live_asks_the_provider_from_the_environment(capsys, monkeypatch, temp_root):
    stub = LiveStub()
    monkeypatch.setattr("mendwright.providers.auto_provider", lambda *a, **k: stub)
    assert cli.main(["demo", "--live"]) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[1] == ("A stand-in page (lxml, no browser) and a live model: LiveStub, model "
                        "stub-model (real API calls).")
    assert lines[8] == ("      model    proposed " + HEALED_XPATH
                        + " (LiveStub, a real API call)")
    assert lines[2:8] == DEMO_LINES[2:8] and lines[9:] == DEMO_LINES[9:]
    assert [b["key"] for b in stub.bundles] == ["accept_terms"]   # pay: no call
    assert captured.err == ""


def test_demo_live_without_a_provider_says_so(capsys, monkeypatch, temp_root):
    def nothing(*args, **kwargs):
        raise ValueError("mendwright.auto_provider: no provider configured. Set one of these.")
    monkeypatch.setattr("mendwright.providers.auto_provider", nothing)
    assert cli.main(["demo", "--live"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "mendwright demo --live: no model provider is configured, so there is nothing to "
        "call. mendwright.auto_provider: no provider configured. Set one of these.",
        "mendwright demo --live: set a provider (mendwright doctor shows what it finds), or "
        "run mendwright demo without --live for the scripted reply."]
    assert list(temp_root.iterdir()) == []


def test_demo_live_when_the_model_fails_is_not_called_a_bug(capsys, monkeypatch, temp_root):
    monkeypatch.setattr("mendwright.providers.auto_provider", lambda *a, **k: LiveStub(None))
    assert cli.main(["demo", "--live"]) == 1
    err = capsys.readouterr().err
    assert "this is a bug" not in err
    assert "with a live model the result depends on the model" in err


def test_story_result(tmp_path):
    lines = []
    result = story(StandInPage(), str(tmp_path / "overrides.json"), say=lines.append)
    assert result.ok
    assert (result.v1_terms, result.v1_pay, result.v2_terms_missed) == (True, True, True)
    assert result.healed_xpath == HEALED_XPATH
    assert (result.retried, result.confirmed) == (True, True)
    assert (result.pay_refused, result.pay_model_calls, result.model_calls) == (True, 0, 1)
    assert result.banner_handled is True
    assert lines == DEMO_LINES[3:-2]


def test_story_needs_the_banner_handled(tmp_path, monkeypatch):
    # Without the interruption the banner covers the form, the retried check() fails
    # in the stand-in page as it would in a browser, and the story is not ok.
    monkeypatch.setattr(story_module, "COOKIE_SELECTOR", "#no-such-button")
    lines = []
    with pytest.raises(RuntimeError, match="intercepts pointer events"):
        story(StandInPage(), str(tmp_path / "overrides.json"), say=lines.append)


def test_scripted_provider_runs_the_real_prompt_and_parser():
    bundle = {"key": "accept_terms", "expectedRole": "checkbox", "expectedText": [],
              "candidates": ["#accept-terms"], "inventory": [], "html": "<p>hi</p>"}
    provider = ScriptedProvider({"accept_terms": SCRIPTED_REPLY})
    assert provider.propose(bundle) == parse_proposal(SCRIPTED_REPLY)
    assert provider.prompts == [build_prompt(bundle)]
    assert provider.last_error is None

    assert provider.propose(dict(bundle, key="pay")) is None
    assert provider.last_error == "the scripted provider has no reply for 'pay'."
    assert provider.calls == ["accept_terms", "pay"]


def test_demo_pages_ship_inside_the_package():
    folder = Path(mendwright.__file__).parent / "demo"
    assert (folder / "v1.html").is_file() and (folder / "v2.html").is_file()
    v1, v2 = page_html("v1"), page_html("v2")
    assert 'id="accept-terms"' in v1 and 'id="pay"' in v1
    assert 'id="accept-terms"' not in v2 and 'id="pay"' not in v2
    assert "Pay now" in v2 and "I accept the terms and conditions" in v2
    assert 'id="cookie-accept"' in v2 and 'id="cookie-accept"' not in v1
    with pytest.raises(ValueError, match="'v1' or 'v2'"):
        page_html("v3")


def test_stand_in_page_behaves_like_a_page():
    from mendwright import InvalidSelector, wrap
    from mendwright.drivers import driver_describe, driver_inventory

    page = StandInPage(page_html("v1"))
    driver = wrap(page)
    assert driver.count("#accept-terms") == 1
    assert driver.count("//input[@type='checkbox']") == 1
    assert driver.count("#missing") == 0
    with pytest.raises(InvalidSelector):
        driver.count("form > button")
    assert driver_describe(driver, "#pay")["role"] == "button"
    assert driver_describe(driver, "#pay")["text"] == "Pay now"
    rows = driver_inventory(driver)
    assert [r["tag"] for r in rows] == ["input", "input", "button"]
    assert all(r["text"] == "" for r in rows if r["tag"] == "input")

    box = page.locator("css=#accept-terms")
    assert box.is_checked() is False
    box.check()
    assert box.is_checked() is True
    box.click()
    assert box.is_checked() is False
    with pytest.raises(RuntimeError, match="matched 0 elements"):
        page.locator("css=#missing").check()

    page.set_content(page_html("v2"))
    assert driver.count("#accept-terms") == 0
    assert driver.count(HEALED_XPATH) == 1

    # The v2 cookie banner covers the form until a click inside it closes it.
    box = page.locator("xpath=" + HEALED_XPATH)
    with pytest.raises(RuntimeError, match='<div id="cookie-banner"> intercepts pointer events'):
        box.check()
    assert box.is_checked() is False
    page.locator("css=#cookie-accept").click()
    assert driver.count("#cookie-banner") == 0
    box.check()
    assert box.is_checked() is True


def test_stand_in_page_has_a_url_you_can_set():
    assert StandInPage().url == "about:blank"
    page = StandInPage(page_html("v1"), url="https://shop.example/checkout")
    assert page.url == "https://shop.example/checkout"
    page.url = "https://shop.example/checkout/review"
    assert mendwright.wrap(page).url() == "https://shop.example/checkout/review"


def test_demo_browser_without_playwright(capsys, monkeypatch, temp_root):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    assert cli.main(["demo", "--browser"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing half-told
    assert captured.err == (
        'mendwright demo: --browser needs Playwright, which is not installed. Install it '
        'with: pip install "mendwright[playwright]", then: python -m playwright install '
        'chromium\n')
    assert list(temp_root.iterdir()) == []


class _FakeManager:
    """Stands in for sync_playwright().start(): each browser type launches `launch()`."""

    def __init__(self, launch):
        self.stopped = False
        for name in ("chromium", "firefox", "webkit"):
            setattr(self, name, types.SimpleNamespace(launch=launch))

    def stop(self):
        self.stopped = True


def _fake_playwright(monkeypatch, launch):
    manager = _FakeManager(launch)
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: types.SimpleNamespace(start=lambda: manager)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return manager


def test_demo_browser_not_installed(capsys, monkeypatch):
    def launch(headless):
        raise RuntimeError("BrowserType.launch: Executable doesn't exist at /x/firefox\n"
                           "Looks like Playwright was just installed or updated.")
    manager = _fake_playwright(monkeypatch, launch)
    assert cli.main(["demo", "--browser", "firefox"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("mendwright demo: Playwright's firefox browser is not installed. "
                            "Install it with: python -m playwright install firefox\n")
    assert manager.stopped


def test_demo_browser_runs_the_same_story_on_the_page_it_gets(capsys, monkeypatch, temp_root):
    closed = []
    browser = types.SimpleNamespace(new_page=StandInPage, close=lambda: closed.append(True))
    seen = []

    def launch(headless):
        seen.append(headless)
        return browser
    monkeypatch.setattr(story_module, "BROWSER_TIMEOUT_MS", 0)
    manager = _fake_playwright(monkeypatch, launch)
    assert cli.main(["demo", "--browser", "webkit"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == ("Real headless webkit (pages loaded with page.set_content) and a "
                      "scripted model reply (no API call).")
    assert out[2:] == DEMO_LINES[2:]
    assert seen == [True] and closed == [True] and manager.stopped
    assert list(temp_root.iterdir()) == []


def test_demo_rejects_an_unknown_browser(capsys):
    assert cli.main(["demo", "--browser", "netscape"]) == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.browser
def test_demo_in_real_chromium(capsys):
    pytest.importorskip("playwright.sync_api")
    assert cli.main(["demo", "--browser", "chromium"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2:] == DEMO_LINES[2:]


def test_python_dash_m_demo_from_a_temp_folder(tmp_path):
    """The command works from any folder, writes nothing there, and pulls in no browser
    library (subprocess, so sys.modules starts clean)."""
    env = _child_env()
    env["NO_COLOR"] = "1"
    probe = ("import runpy, sys; sys.argv = ['mendwright', 'demo']\n"
             "try:\n    runpy.run_module('mendwright', run_name='__main__')\n"
             "except SystemExit as exc:\n    code = exc.code\n"
             "loaded = sorted(m for m in sys.modules if m.split('.')[0] in "
             "('playwright', 'selenium', 'patchright', 'camoufox'))\n"
             "print('exit', code, 'loaded', loaded)\n")
    proc = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[:-1] == DEMO_LINES
    assert lines[-1] == "exit 0 loaded []"
    assert list(tmp_path.iterdir()) == []


# --- doctor -----------------------------------------------------------------------------

def test_doctor_offline_with_no_provider(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("mendwright " + mendwright.__version__ + "  (")
    assert lines[1].startswith("Python " + sys.version.split()[0])
    assert lines[2].startswith("lxml ")
    assert "Browser libraries (looked up only, nothing imported or launched)" in lines
    for name in cli.BROWSER_LIBRARIES:
        assert any(line.startswith("  " + name.ljust(11) + " ") for line in lines)
    assert ("  none configured, so only fingerprint healing (no model call) can repair a broken "
            "selector.") in lines
    assert "Why: mendwright.auto_provider: no provider configured." in out
    assert "Fingerprint healing (no model)" in lines
    assert "  on: each time a selector matches, mendwright records what the element looks" in out
    store = tmp_path / ".mendwright" / "overrides.json"
    assert "  " + str(store) in lines
    assert ("  the folder does not exist yet; it is created the first time mendwright.open() "
            "records a selector") in lines
    assert "  " + str(tmp_path) + " is writable" in lines
    assert "  no MENDWRIGHT_* variables are set" in lines
    assert "Problems" not in lines
    assert list(tmp_path.iterdir()) == []  # doctor creates nothing


def test_doctor_with_the_fingerprint_tier_off(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MENDWRIGHT_FINGERPRINT", "0")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "  none configured, so a broken selector is reported but not repaired." in lines
    assert "  off (MENDWRIGHT_FINGERPRINT=0), so only a model provider can repair a broken" in out
    assert "  MENDWRIGHT_FINGERPRINT=0" in lines
    assert "Problems" not in lines

    monkeypatch.setenv("MENDWRIGHT_FINGERPRINT", "maybe")
    assert cli.main(["doctor", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["fingerprint"] == {"enabled": True, "source": "default"}
    assert any("MENDWRIGHT_FINGERPRINT must be 1 or 0" in p for p in data["problems"])

    monkeypatch.setenv("MENDWRIGHT_FINGERPRINT", "1")
    monkeypatch.setenv("MENDWRIGHT_DISABLE", "1")
    assert cli.main(["doctor"]) == 0
    assert "  off, because MENDWRIGHT_DISABLE is on. Unset it to heal again." in capsys.readouterr().out


def test_doctor_json(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["doctor", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mendwright"]["version"] == mendwright.__version__
    assert data["python"]["version"] == sys.version.split()[0]
    assert [row["name"] for row in data["browser_libraries"]] == list(cli.BROWSER_LIBRARIES)
    assert data["provider"]["configured"] is False
    assert data["provider"]["reason"].startswith("mendwright.auto_provider: no provider")
    assert data["store"]["path"] == str(tmp_path / ".mendwright" / "overrides.json")
    assert data["store"]["source"] == "default"
    assert data["store"]["writable"] is True
    assert data["fingerprint"] == {"enabled": True, "source": "default"}
    assert data["env"] == {} and data["problems"] == []


def test_doctor_never_prints_secrets(capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-0123456789")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test-model")
    monkeypatch.setenv("MENDWRIGHT_KEY", "mw-SECRET-download-key")
    monkeypatch.setenv("MENDWRIGHT_GATEWAY",
                       "https://user:pw-SECRET@gw.example.test:8443/heal?token=SECRET-q")
    assert cli.main(["doctor"]) == 0
    assert cli.main(["doctor", "--json"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out
    assert "  AnthropicProvider, model claude-test-model" in out
    assert "  chosen because ANTHROPIC_API_KEY is set" in out
    assert "MENDWRIGHT_GATEWAY=https://gw.example.test:8443" in out
    assert "MENDWRIGHT_KEY=(set, value hidden)" in out
    assert ("provider variables set: ANTHROPIC_API_KEY, ANTHROPIC_MODEL, MENDWRIGHT_GATEWAY, "
            "MENDWRIGHT_KEY (values not shown)") in out


def test_doctor_scrubs_secrets_from_a_provider_error(capsys, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SECRET-abcdef")

    def broken(*args, **kwargs):
        raise ValueError("bad key sk-SECRET-abcdef for OpenAI")
    monkeypatch.setattr("mendwright.providers.auto_provider", broken)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out
    assert "Why: bad key *** for OpenAI" in out


def test_doctor_shows_where_the_provider_sends_the_page(capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-0123456789")
    monkeypatch.setenv("ANTHROPIC_MODEL", "m")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://user:pw@proxy.corp.example/llm?k=1")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert ("  endpoint: https://proxy.corp.example/llm (heal requests go here)"
            in out)
    assert ("provider variables set: ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_BASE_URL "
            "(values not shown)") in out
    assert "pw" not in out and "SECRET" not in out


def test_doctor_names_ollama_and_its_host(capsys, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "fake")
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:18734")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "  Ollama (OpenAI-compatible), model fake" in out
    assert "  endpoint: http://127.0.0.1:18734/v1 (heal requests go here)" in out


def test_doctor_gateway_provider(capsys, monkeypatch):
    monkeypatch.setenv("MENDWRIGHT_GATEWAY", "https://heal.example.test")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert ("  GatewayProvider, gateway https://heal.example.test (the model is set on the "
            "gateway)") in out
    assert "  chosen because MENDWRIGHT_GATEWAY is set" in out


def test_doctor_explains_a_half_configured_provider(capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-0123456789")
    assert cli.main(["doctor", "--json"]) == 0
    provider = json.loads(capsys.readouterr().out)["provider"]
    assert provider["configured"] is False
    assert "ANTHROPIC_MODEL" in provider["reason"]
    assert provider["selected_by"] == ["ANTHROPIC_API_KEY"]


def test_doctor_reports_bad_settings_and_still_exits_0(capsys, monkeypatch):
    monkeypatch.setenv("MENDWRIGHT_TIMEOUT_MS", "soon")
    monkeypatch.setenv("MENDWRIGHT_DISABLE", "maybe")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out.splitlines()
    problems = out[out.index("Problems") + 1:]
    assert any("MENDWRIGHT_TIMEOUT_MS must be a whole number" in line for line in problems)
    assert any("MENDWRIGHT_DISABLE must be 1 or 0" in line for line in problems)
    assert "  MENDWRIGHT_TIMEOUT_MS=soon" in out


def test_doctor_disable_note(capsys, monkeypatch):
    monkeypatch.setenv("MENDWRIGHT_DISABLE", "1")
    assert cli.main(["doctor"]) == 0
    assert ("  MENDWRIGHT_DISABLE is on, so mendwright.open() heals nothing and calls no "
            "provider. Unset it to heal again.") in capsys.readouterr().out


def test_doctor_store_from_env(capsys, tmp_path, monkeypatch):
    store = tmp_path / "custom" / "store.json"
    store.parent.mkdir()
    store.write_text(json.dumps({"schema": 2, "selectors": {"a": {"candidates": []},
                                                            "b": {"candidates": []}}}))
    (store.parent / "journal.jsonl").write_text("")
    monkeypatch.setenv("MENDWRIGHT_STORE", str(store))
    assert cli.main(["doctor"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "  " + str(store) in lines
    assert "  (from MENDWRIGHT_STORE)" in lines
    assert ("  exists, 2 key(s), journal.jsonl next to it. See it with: mendwright report"
            in lines)
    assert "  MENDWRIGHT_STORE=" + str(store) in lines


def test_doctor_imports_no_browser_library(capsys, monkeypatch):
    before = set(sys.modules)
    assert cli.main(["doctor", "--json"]) == 0
    new = {m.split(".")[0] for m in set(sys.modules) - before}
    assert not new & set(cli.BROWSER_LIBRARIES)


def test_doctor_shows_a_missing_library(capsys, monkeypatch):
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "selenium" else real(name, *a))
    assert cli.main(["doctor"]) == 0
    assert "  selenium    not installed" in capsys.readouterr().out.splitlines()


# --- report -----------------------------------------------------------------------------

UNION = "//button[@id='go'] | //a[@id='go']"


def _write_store(folder, journal=True):
    folder.mkdir(parents=True, exist_ok=True)
    store = folder / "overrides.json"
    store.write_text(json.dumps({"schema": 2, "selectors": {
        "accept_terms": {"expectedRole": "checkbox", "candidates": [
            {"xpath": "#accept-terms", "source": "code", "status": "active",
             "lastSucceededAt": "2026-10-01T08:00:00+00:00"},
            {"xpath": "//label//input[@type='checkbox']", "source": "heal",
             "status": "active", "lastSucceededAt": "2026-10-02T09:30:00+00:00"},
            {"xpath": "//input[@name='terms']", "source": "heal", "status": "quarantined",
             "lastSucceededAt": None},
        ]},
        "go": {"expectedRole": "button", "destructive": True, "candidates": [
            {"xpath": UNION, "source": "seed", "status": "active", "lastSucceededAt": None},
            {"xpath": "#old-go", "source": "code", "status": "superseded",
             "lastSucceededAt": None, "frameXPath": "//iframe[@id='shop']"},
        ]},
        "pay_button": {"expectedRole": "button", "candidates": []},
    }}), encoding="utf-8")
    if journal:
        events = [
            {"ts": "2026-10-02T09:29:59+00:00", "event": "heal.requested", "key": "accept_terms"},
            {"ts": "2026-10-02T09:30:00+00:00", "event": "heal.applied", "key": "accept_terms",
             "provider": "AnthropicProvider", "model": "claude-test", "latency_ms": 1400,
             "xpath": "//input[@name='terms']", "old": ["#accept-terms"]},
            {"ts": "2026-10-02T09:30:01+00:00", "event": "heal.canary", "key": "accept_terms",
             "success": False},
            {"ts": "2026-10-02T09:30:01+00:00", "event": "heal.quarantine",
             "key": "accept_terms", "xpath": "//input[@name='terms']"},
            {"ts": "2026-10-03T10:00:00+00:00", "event": "heal.requested", "key": "accept_terms"},
            {"ts": "2026-10-03T10:00:02+00:00", "event": "heal.rejected", "key": "accept_terms",
             "reason": "not unique (2)"},
            {"ts": "2026-10-03T10:00:03+00:00", "event": "heal.applied", "key": "accept_terms",
             "provider": "AnthropicProvider", "model": "claude-test", "latency_ms": 900,
             "xpath": "//label//input[@type='checkbox']", "old": ["#accept-terms", "x", "y"]},
            {"ts": "2026-10-04T10:00:00+00:00", "event": "heal.requested", "key": "go"},
        ]
        lines = [json.dumps(e) for e in events]
        lines.insert(3, "{not json")
        (folder / "journal.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return store


def test_report_text(capsys, tmp_path):
    store = _write_store(tmp_path / "s")
    assert cli.main(["report", "--store", str(store)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Store: " + str(store),
        "3 key(s), 5 selector(s)",
        "",
        "accept_terms  (checkbox)",
        "  active      code  #accept-terms  (last success: 2026-10-01 08:00:00 UTC)",
        "  active      heal  //label//input[@type='checkbox']  "
        "(last success: 2026-10-02 09:30:00 UTC)",
        "  quarantined heal  //input[@name='terms']  (last success: never)",
        "  heals: 2 requested, 2 applied, 1 rejected, 1 quarantined",
        "",
        "go  (button, destructive, never auto-healed)",
        "  active      seed  " + UNION + "  (last success: never)",
        "  superseded  code  #old-go  in frame //iframe[@id='shop']  (last success: never)",
        "  heals: 1 requested, 0 applied, 0 rejected, 0 quarantined",
        "",
        "pay_button  (button)",                    # the word 'pay' alone marks nothing
        "",
        "Heals (journal.jsonl): 3 requested, 2 applied, 1 rejected, 1 quarantined",
        "Last applied heals, newest first:",
        "  2026-10-03 10:00:03 UTC  accept_terms  (AnthropicProvider, claude-test)",
        "    #accept-terms (+2 more) -> //label//input[@type='checkbox']",
        "  2026-10-02 09:30:00 UTC  accept_terms  (AnthropicProvider, claude-test)",
        "    #accept-terms -> //input[@name='terms']",
    ]


def test_report_markdown_for_the_step_summary(capsys, tmp_path):
    store = _write_store(tmp_path / "s")
    assert cli.main(["report", "--store", str(store), "--format", "md"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "## mendwright heal report",
        "",
        "Store: `" + str(store) + "` with 3 key(s) and 5 selector(s).",
        "",
        "| Key | Selector | Source | Status | Last success |",
        "| --- | --- | --- | --- | --- |",
        "| `accept_terms` (checkbox) | `#accept-terms` | code | active | "
        "2026-10-01 08:00:00 UTC |",
        "|  | `//label//input[@type='checkbox']` | heal | active | 2026-10-02 09:30:00 UTC |",
        "|  | `//input[@name='terms']` | heal | **quarantined** | never |",
        "| `go` (button, destructive, never auto-healed) | "
        "`//button[@id='go'] \\| //a[@id='go']` | seed | active | never |",
        "|  | `#old-go` in frame `//iframe[@id='shop']` | code | **superseded** | never |",
        "| `pay_button` (button) | (none) | | | |",
        "",
        "### Heals",
        "",
        "| Requested | Applied | Rejected | Quarantined |",
        "| ---: | ---: | ---: | ---: |",
        "| 3 | 2 | 1 | 1 |",
        "",
        "Last applied heals, newest first:",
        "",
        "| When | Key | Old | New | Provider |",
        "| --- | --- | --- | --- | --- |",
        "| 2026-10-03 10:00:03 UTC | `accept_terms` | `#accept-terms (+2 more)` | "
        "`//label//input[@type='checkbox']` | AnthropicProvider, claude-test |",
        "| 2026-10-02 09:30:00 UTC | `accept_terms` | `#accept-terms` | "
        "`//input[@name='terms']` | AnthropicProvider, claude-test |",
    ]


def test_report_markdown_rows_have_five_cells(capsys, tmp_path):
    """An XPath union's '|' must not split a table row."""
    store = _write_store(tmp_path / "s")
    cli.main(["report", "--store", str(store), "--format", "md"])
    rows = [line for line in capsys.readouterr().out.splitlines()
            if line.startswith("| ") and "---" not in line]
    for row in rows:
        cells = row.replace("\\|", "").strip("|").split("|")
        assert len(cells) in (4, 5), row


def test_report_json(capsys, tmp_path):
    store = _write_store(tmp_path / "s")
    assert cli.main(["report", "--store", str(store), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["store"] == str(store)
    assert [k["key"] for k in data["keys"]] == ["accept_terms", "go", "pay_button"]
    go = data["keys"][1]
    assert go["destructive"] is True
    assert go["candidates"][1] == {"selector": "#old-go", "source": "code",
                                   "status": "superseded", "last_success": None,
                                   "added": None, "frame": "//iframe[@id='shop']"}
    assert data["keys"][2]["destructive"] is False        # a key is destructive only when marked
    journal = data["journal"]
    assert journal["exists"] is True
    assert journal["counts"] == {"requested": 3, "applied": 2, "rejected": 1, "quarantined": 1}
    assert journal["per_key"]["go"]["requested"] == 1
    assert [h["new"] for h in journal["last_applied"]] == [
        "//label//input[@type='checkbox']", "//input[@name='terms']"]
    assert journal["last_applied"][0]["latency_ms"] == 900


def test_report_without_a_journal(capsys, tmp_path):
    store = _write_store(tmp_path / "s", journal=False)
    assert cli.main(["report", "--store", str(store)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "No journal.jsonl next to the store, so there are no heal counts."
    assert cli.main(["report", "--store", str(store), "--format", "md"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "No `journal.jsonl` next to the store, so there are no heal counts."


def test_report_uses_mendwright_store_by_default(capsys, tmp_path, monkeypatch):
    store = _write_store(tmp_path / "s")
    monkeypatch.setenv("MENDWRIGHT_STORE", str(store))
    assert cli.main(["report"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "Store: " + str(store)


def test_report_missing_store_exits_1(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["report"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    default = tmp_path / ".mendwright" / "overrides.json"
    assert captured.err == (
        "mendwright report: no store at " + str(default) + ". Run your tests with mendwright "
        "first, or pass --store PATH (the default is MENDWRIGHT_STORE, else "
        "./.mendwright/overrides.json).\n")

    assert cli.main(["report", "--store", str(tmp_path / "nope.json")]) == 1
    assert capsys.readouterr().err == (
        "mendwright report: no store at " + str(tmp_path / "nope.json") + ". Check the path "
        "given to --store; the file is created by the first heal.\n")


def test_report_unreadable_store_exits_1(capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    assert cli.main(["report", "--store", str(bad)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("mendwright report: " + str(bad) + " is not valid JSON (")
    assert "Fix the file, or delete it to start an empty store." in err

    other = tmp_path / "other.json"
    other.write_text("[1, 2]", encoding="utf-8")
    assert cli.main(["report", "--store", str(other)]) == 1
    assert "is not a mendwright store" in capsys.readouterr().err


def test_report_reads_a_schema_1_store(capsys, tmp_path):
    store = tmp_path / "old.json"
    store.write_text(json.dumps({"selectors": [{"key": "save", "xpath": "//button[@id='save']"}]}))
    assert cli.main(["report", "--store", str(store), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["keys"][0]["key"] == "save"
    assert data["keys"][0]["candidates"][0]["source"] == "seed"


def test_report_empty_store(capsys, tmp_path):
    store = tmp_path / "empty.json"
    store.write_text('{"schema": 2, "selectors": {}}')
    assert cli.main(["report", "--store", str(store)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "The store has no keys yet." in out
