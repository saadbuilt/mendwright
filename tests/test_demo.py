"""examples/demo/demo.py must run offline and go red -> green through the public facade.

It is the example a reader copies from, so it has to keep working: v1 matches, v2 (a
redesign) breaks the selector, and one heal.locator() call heals and retries it. The test
imports the script by path (examples/ is not a package) and runs demo.main(). The page is
mendwright.demo.StandInPage and the provider mendwright.demo.ScriptedProvider: no
network, no API key, no browser.
"""

import ast
import importlib.util
import json
from pathlib import Path

from mendwright.demo import HEALED_XPATH

_DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo" / "demo.py"


def _load_demo():
    spec = importlib.util.spec_from_file_location("mendwright_breaking_demo", _DEMO)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_heals_red_to_green(tmp_path, capsys):
    demo = _load_demo()
    report = demo.main(override_path=str(tmp_path / "override.json"))

    assert report["v1_resolved"] is True             # the selector matched v1
    assert report["v2_broke"] is True                # and stopped matching on v2
    assert report["v2_resolved_after_heal"] is True  # the healed selector checked the box
    assert report["healed"] is True

    # What was applied is the label-anchored XPath, not the id selector it replaced.
    assert report["healed_xpath"] == demo.HEALED_XPATH == HEALED_XPATH
    assert report["healed_xpath"] != demo.STALE_SELECTOR

    # The provider got the real prompt, and expect= confirmed the heal.
    assert report["provider_prompted"] is True
    assert report["canary_outcome"] is True

    out = capsys.readouterr().out.splitlines()
    assert out[0] == "v1  #accept-terms matched, box checked"
    assert out[1] == "v2  #accept-terms no longer matches"
    assert out[2] == "    healed: #accept-terms -> " + HEALED_XPATH + " (confirmed)"
    assert out[3] == "result: red -> green"


def test_demo_saves_the_heal_in_the_store_it_was_given(tmp_path):
    demo = _load_demo()
    store = tmp_path / "override.json"
    demo.main(override_path=str(store))
    saved = json.loads(store.read_text(encoding="utf-8"))
    candidates = {c["xpath"]: c for c in saved["selectors"]["accept_terms"]["candidates"]}
    assert set(candidates) == {demo.STALE_SELECTOR, HEALED_XPATH}  # in recency order
    assert candidates[HEALED_XPATH]["source"] == "heal"
    assert candidates[demo.STALE_SELECTOR]["source"] == "code"
    assert saved["selectors"]["accept_terms"]["expectedRole"] == "checkbox"  # read from v1
    assert (tmp_path / "journal.jsonl").exists()


def test_demo_is_deterministic(tmp_path):
    """Two runs produce the same report: no clock or random value leaks into it."""
    demo = _load_demo()
    first = demo.main(override_path=str(tmp_path / "a.json"))
    second = demo.main(override_path=str(tmp_path / "b.json"))
    assert first == second


def test_demo_main_exit_contract(tmp_path):
    """main() returns a report whose 'healed' flag __main__ turns into exit code 0."""
    demo = _load_demo()
    report = demo.main(override_path=str(tmp_path / "override.json"))
    assert set(report) == {
        "v1_resolved", "v2_broke", "healed", "healed_xpath",
        "v2_resolved_after_heal", "provider_prompted", "canary_outcome",
    }
    assert report["healed"] is True


def test_demo_uses_only_the_public_facade():
    """The example is what a reader copies, so it sticks to mendwright.open() and the
    stand-ins from mendwright.demo, not the low-level core."""
    tree = ast.parse(_DEMO.read_text(encoding="utf-8"))
    used = {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "mendwright"}
    imported = {(node.module, alias.name) for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mendwright")
                for alias in node.names}
    assert used == {"open"}
    assert {module for module, _ in imported} == {"mendwright.demo"}
