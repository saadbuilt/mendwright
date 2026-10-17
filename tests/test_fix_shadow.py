"""`mendwright fix` and a heal inside an open shadow root. The heal is saved as the Playwright
selector chain of its hosts and its CSS (see mendwright.shadow), which only Playwright runs, so
mendwright fix writes it only where the old selector is passed to a Playwright page or locator
method in a Python file, and lists every other place with the chain to write by hand.
Offline, as tests/test_fix.py."""

import json

import pytest

from mendwright import cli, fixback

T0 = "2026-10-01T08:00:00+00:00"
T1 = "2026-10-02T09:30:00+00:00"
HOST = 'my-app[id="root"]'
CHAIN = HOST + ' >> [data-testid="save"]'


def cand(xpath, source, last=None, status="active", **extra):
    c = {"xpath": xpath, "source": source, "status": status, "addedAt": T0,
         "lastSucceededAt": last}
    c.update(extra)
    return c


def store(key="#save", old="#save", new=CHAIN, hosts=(HOST,)):
    return {key: {"expectedRole": "button", "candidates": [
        cand(old, "code", T0), cand(new, "heal", T1, shadowHosts=list(hosts))]}}


def canary(key, xpath):
    return {"ts": T1, "event": "heal.canary", "key": key, "xpath": xpath, "success": True}


def write_store(root, selectors, events):
    folder = root / ".mendwright"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "overrides.json").write_text(json.dumps({"schema": 2, "selectors": selectors}),
                                           encoding="utf-8")
    (folder / "journal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events),
                                          encoding="utf-8")


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def run(capsys, *args):
    code = cli.main(["fix", *args])
    return code, capsys.readouterr().out


@pytest.fixture
def root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def test_a_shadow_heal_is_written_into_a_playwright_call_as_its_chain(root, capsys):
    write_store(root, store(), [canary("#save", CHAIN)])
    source = write(root, "test_a.py", 'page.locator("#save").click()\npage.click("#save")\n')
    code, out = run(capsys, "--apply")
    assert code == 0
    assert source.read_text(encoding="utf-8") == (
        "page.locator('" + CHAIN + "').click()\npage.click('" + CHAIN + "')\n")
    assert "  now  " + CHAIN in out


@pytest.mark.parametrize("line", [
    'heal.locator("save", "#save").click()\n',                          # may run on Selenium
    'driver.find_element(By.CSS_SELECTOR, "#save")\n',
    'SAVE = "#save"\n',
], ids=["heal-locator", "find-element", "constant"])
def test_anywhere_else_the_shadow_heal_is_listed_with_the_chain(root, capsys, line):
    write_store(root, store(key="save"), [canary("save", CHAIN)])
    source = write(root, "test_a.py", line)
    before = source.read_bytes()
    code, out = run(capsys, "--apply")
    assert code == 0 and source.read_bytes() == before
    note = [lin for lin in out.splitlines() if "test_a.py:1 was not changed" in lin][0]
    assert "inside the shadow root of " + HOST in note
    assert "change it by hand to: " + CHAIN in note
    assert "Selenium's find_element() cannot reach into a shadow root" in note


def test_a_shadow_heal_is_never_written_into_another_kind_of_file(root, capsys):
    write_store(root, store(), [canary("#save", CHAIN)])
    source = write(root, "a.spec.ts", "await page.locator('#save').click();\n")
    before = source.read_bytes()
    out = run(capsys, "--apply", "--glob", "*.ts")[1]
    assert source.read_bytes() == before
    assert "a.spec.ts:1 was not changed" in out


def test_a_key_of_heal_driver_with_a_shadow_heal_is_left_alone(root, capsys):
    write_store(root, store(key="css selector=#save"), [canary("css selector=#save", CHAIN)])
    source = write(root, "test_a.py", 'driver.find_element(By.CSS_SELECTOR, "#save")\n')
    before = source.read_bytes()
    out = run(capsys, "--apply")[1]
    assert source.read_bytes() == before
    assert ("css selector=#save: it comes from driver.find_element() through heal.driver, and its "
            "heal sits inside the shadow root of " + HOST + ", which Selenium's find_element() "
            "cannot reach.") in out


@pytest.mark.parametrize("new, hosts", [
    ('#save', [HOST]),                              # not a chain
    ('other-app >> #save', [HOST]),                 # another host than it says
    (HOST + " >> //button", [HOST]),                # XPath, which runs inside no shadow root
    ('div >> #save', ['div']),                      # a host mendwright does not build
])
def test_a_shadow_heal_mendwright_could_not_have_saved_is_left_alone(root, capsys, new, hosts):
    write_store(root, store(new=new, hosts=hosts), [canary("#save", new)])
    source = write(root, "test_a.py", 'page.locator("#save")\n')
    before = source.read_bytes()
    out = run(capsys, "--apply")[1]
    assert source.read_bytes() == before
    assert "is not one mendwright saves" in out


def test_the_json_names_the_hosts_and_the_left_alone_codes(root, capsys):
    write_store(root, {**store(), **store(key="css selector=#x", old="#x")},
                [canary("#save", CHAIN), canary("css selector=#x", CHAIN)])
    write(root, "test_a.py", 'page.locator("#save")\nheal.locator("k2", "#save")\n')
    code = cli.main(["fix", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["written_back"][0]["shadow"] == [HOST]
    assert {i["code"] for i in data["left_alone"]} == {"shadow_driver", "shadow_call"}
    assert "shadow_driver" in fixback.CODES and "shadow_call" in fixback.CODES


def test_a_selector_followed_into_a_shadow_root_is_written_with_its_own_css(root, capsys):
    followed = HOST + ' >> .panel > button'
    write_store(root, store(key="//button[@id='save']", old="//button[@id='save']", new=followed),
                [canary("//button[@id='save']", followed)])
    source = write(root, "test_a.py", "page.click(\"//button[@id='save']\")\n")
    assert run(capsys, "--apply")[0] == 0
    assert source.read_text(encoding="utf-8") == "page.click('" + followed + "')\n"


@pytest.mark.parametrize("line, written", [
    ("page.locator(\"xpath=//button[@id='save']\").click()\n", True),     # heal.page's key
    ("driver.find_element(By.XPATH, \"//button[@id='save']\")\n", False),  # heal.driver's
], ids=["heal-page", "find-element"])
def test_a_key_written_as_an_xpath_is_fixed_only_in_a_playwright_call(root, capsys, line, written):
    key = "xpath=//button[@id='save']"
    write_store(root, store(key=key, old=key), [canary(key, CHAIN)])
    source = write(root, "test_a.py", line)
    out = run(capsys, "--apply")[1]
    assert (CHAIN in source.read_text(encoding="utf-8")) is written
    assert ("change it by hand to: " + CHAIN in out) is not written
