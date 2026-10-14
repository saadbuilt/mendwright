"""`mendwright report --format html`: one self-contained page from a store and its journal.

Offline: the page is built from tmp_path files and parsed with the standard library's
HTMLParser. Nothing here needs a browser; the layout at 1280 and 390 px was checked by hand
in headless Chromium.
"""

import json
import re
from html.parser import HTMLParser

import pytest

from mendwright import cli, report_html

URL_SELECTOR = "//a[@href='/x']"
TERMS = "//label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox']"


def write_sample(folder, journal=True, extra_events=()):
    folder.mkdir(parents=True, exist_ok=True)
    store = folder / "overrides.json"
    store.write_text(json.dumps({"schema": 2, "selectors": {
        "accept_terms": {"expectedRole": "checkbox", "candidates": [
            {"xpath": "#accept-terms", "source": "code", "status": "active",
             "lastSucceededAt": "2026-10-01T08:00:00+00:00"},
            {"xpath": TERMS, "source": "heal", "status": "active",
             "lastSucceededAt": "2026-10-02T09:30:00+00:00"},
            {"xpath": "//input[@name='terms']", "source": "heal", "status": "quarantined",
             "lastSucceededAt": None},
        ]},
        "go": {"expectedRole": "button", "destructive": True, "candidates": [
            {"xpath": "//button[@id='go'] | //a[@id='go']", "source": "seed", "status": "active",
             "lastSucceededAt": None},
            {"xpath": "#old-go", "source": "code", "status": "superseded",
             "lastSucceededAt": None, "frameXPath": "//iframe[@id='shop']"},
        ]},
        "empty_key": {"expectedRole": "button", "candidates": []},
    }}), encoding="utf-8")
    if journal:
        events = [
            {"ts": "2026-10-02T09:29:59+00:00", "event": "heal.requested", "key": "accept_terms"},
            {"ts": "2026-10-02T09:30:00+00:00", "event": "heal.applied", "key": "accept_terms",
             "provider": "AnthropicProvider", "model": "claude-test", "latency_ms": 1400,
             "xpath": "//input[@name='terms']", "old": ["#accept-terms"]},
            {"ts": "2026-10-02T09:30:01+00:00", "event": "heal.canary", "key": "accept_terms",
             "xpath": "//input[@name='terms']", "success": False},
            {"ts": "2026-10-02T09:30:01+00:00", "event": "heal.quarantine", "key": "accept_terms",
             "xpath": "//input[@name='terms']"},
            {"ts": "2026-10-03T10:00:00+00:00", "event": "heal.requested", "key": "accept_terms"},
            {"ts": "2026-10-03T10:00:02+00:00", "event": "heal.rejected", "key": "accept_terms",
             "reason": "not unique (2)"},
            {"ts": "2026-10-03T10:00:03+00:00", "event": "heal.applied", "key": "accept_terms",
             "provider": "AnthropicProvider", "model": "claude-test", "latency_ms": 900,
             "xpath": TERMS, "old": ["#accept-terms", "x", "y"]},
            {"ts": "2026-10-03T10:00:04+00:00", "event": "heal.canary", "key": "accept_terms",
             "xpath": TERMS, "success": True},
            {"ts": "2026-10-04T10:00:00+00:00", "event": "heal.applied", "key": "go",
             "provider": "OpenAIProvider", "xpath": "//button[@id='g2']", "old": []},
            {"ts": "2026-10-04T11:00:00+00:00", "event": "flow.interrupt", "key": "cookies",
             "before": "accept_terms", "action": "click", "preset": "cookies"},
            {"ts": "2026-10-04T11:00:01+00:00", "event": "flow.interrupt", "key": "newsletter",
             "before": "go", "action": "click", "selector": "#close"},
            {"ts": "2026-10-04T11:00:02+00:00", "event": "flow.skip", "key": "promo",
             "reason": "optional"},
            {"ts": "2026-10-04T11:00:03+00:00", "event": "flow.page_guard", "key": "go",
             "last_page": "/checkout", "page": "/cart"},
        ] + list(extra_events)
        (folder / "journal.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return store


def page(capsys, *args):
    assert cli.main(["report", "--format", "html", *args]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return captured.out


BLOCK = {"div", "p", "li", "ul", "h1", "h2", "td", "th", "tr", "table", "section", "header",
         "footer", "main", "caption", "thead", "tbody"}


def squash(text):
    return " ".join(text.split())


class Page(HTMLParser):
    """The tags, the visible text (block elements end a word) and the table rows of a page."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags, self.tables = [], {}
        self._chunks = []
        self._in_style = False
        self._caption = self._row = self._cell = None
        self._table = ""
        self.feed(text)
        self.close()

    def _gap(self):
        self._chunks.append(" ")
        if self._cell is not None:
            self._cell.append(" ")

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag in BLOCK:
            self._gap()
        if tag == "style":
            self._in_style = True
        elif tag == "caption":
            self._caption = []
        elif tag == "tr":
            self._row = []
        elif tag == "td":
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False
        elif tag == "caption":
            self._table, self._caption = squash("".join(self._caption)), None
        elif tag == "td":
            self._row.append(squash("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row:
            self.tables.setdefault(self._table, []).append(self._row)
            self._row = None
        if tag in BLOCK:
            self._gap()

    def handle_data(self, data):
        if self._in_style:
            return
        self._chunks.append(data)
        if self._caption is not None:
            self._caption.append(data)
        if self._cell is not None:
            self._cell.append(data)

    @property
    def text(self):
        return squash("".join(self._chunks))


def cards(text):
    return {m.group(2): m.group(1) for m in re.finditer(
        r'<div class="num">([^<]*)</div><div class="label">([^<]*)</div>', text)}


@pytest.fixture
def sample(tmp_path):
    return write_sample(tmp_path / "s")


# --- the page ----------------------------------------------------------------------------

def test_the_page_is_self_contained_and_follows_the_reader(capsys, sample):
    out = page(capsys, "--store", str(sample))
    assert out.startswith("<!doctype html>\n<html lang=\"en\">")
    assert '<meta charset="utf-8">' in out
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in out
    assert '<meta name="color-scheme" content="light dark">' in out
    assert "@media (prefers-color-scheme:dark)" in out
    assert "@media (max-width:760px)" in out
    assert "<title>mendwright heal report</title>" in out
    for forbidden in ("<script", "<link", "<img", "<iframe", "<object", "<embed", "<form", "src=",
                      "href=", "url(", "@import", "http://", "https://", "javascript:", "onclick=",
                      "onload="):
        assert forbidden not in out, forbidden
    assert out.isascii()
    assert set(Page(out).tags) <= {
        "html", "head", "meta", "title", "style", "body", "div", "header", "h1", "p", "code",
        "main", "section", "h2", "table", "caption", "thead", "tr", "th", "tbody", "td", "span",
        "ul", "li", "footer"}
    assert "mendwright " + cli._version() in out


def page_text(capsys, sample):
    return page(capsys, "--store", str(sample))


def test_there_is_one_h1_and_a_heading_for_each_part(capsys, sample):
    parsed = Page(page_text(capsys, sample))
    assert parsed.tags.count("h1") == 1
    headings = re.findall(r"<h2 id=\"\w+\">([^<]+)</h2>", page_text(capsys, sample))
    assert headings == ["Summary", "Keys and selectors", "Recent heals", "Flow events",
                        "Quarantines"]


def test_summary_counts(capsys, sample):
    counts = cards(page(capsys, "--store", str(sample)))
    assert counts == {
        "keys": "3", "selectors": "5", "quarantined now": "1", "heals asked for": "2",
        "heals applied": "3", "confirmed": "1", "proposals rejected": "1",
        "interruptions handled": "2", "steps skipped": "1", "page guard stops": "1"}


def test_the_keys_table_has_role_destructive_mark_selectors_and_last_success(capsys, sample):
    parsed = Page(page(capsys, "--store", str(sample)))
    rows = parsed.tables["Keys and their selectors"]
    assert [r[0].split(" ")[0] for r in rows] == ["accept_terms", "empty_key", "go"]
    accept, empty, go = rows
    assert accept[1] == "checkbox"
    assert "code active #accept-terms last success 2026-10-01 08:00:00 UTC" in accept[2]
    assert "heal active " + TERMS + " last success 2026-10-02 09:30:00 UTC" in accept[2]
    assert "heal quarantined //input[@name='terms'] last success never" in accept[2]
    assert accept[3] == "2 asked, 2 applied 1 rejected, 1 quarantined"
    assert empty[2] == "no selectors saved" and empty[3] == "none"
    assert go[0] == "go destructive, never auto-healed"
    assert "seed active //button[@id='go'] | //a[@id='go'] last success never" in go[2]
    assert "code superseded #old-go inside frame //iframe[@id='shop'] last success never" in go[2]
    assert go[3] == "0 asked, 1 applied 0 rejected, 0 quarantined"


def test_recent_heals_show_old_and_new_state_provider_model_and_latency(capsys, sample):
    parsed = Page(page(capsys, "--store", str(sample)))
    rows = parsed.tables["Recent applied heals"]
    assert [r[1] for r in rows] == ["go", "accept_terms", "accept_terms"]        # newest first
    assert rows[0] == ["2026-10-04 10:00:00 UTC", "go", "old (none) new //button[@id='g2']",
                       "not confirmed yet", "OpenAIProvider", "-"]
    assert rows[1] == ["2026-10-03 10:00:03 UTC", "accept_terms",
                       "old #accept-terms (+2 more) new " + TERMS, "confirmed",
                       "AnthropicProvider claude-test", "900 ms"]
    assert rows[2] == ["2026-10-02 09:30:00 UTC", "accept_terms",
                       "old #accept-terms new //input[@name='terms']", "failed, quarantined",
                       "AnthropicProvider claude-test", "1.4 s"]


def test_flow_events_name_the_step_and_what_happened(capsys, sample):
    parsed = Page(page(capsys, "--store", str(sample)))
    rows = parsed.tables["Flow events"]
    assert [r[0] for r in rows] == ["page guard", "skipped step", "interruption", "interruption"]
    assert rows[0][3] == ("Heal declined. The key was last found on /checkout and the page is "
                          "now /cart.")
    assert rows[1][3] == "Skipped instead of healed: optional."
    assert rows[2][1:] == ["2026-10-04 11:00:01 UTC", "newsletter",
                           "Handled before go: click via selector #close."]
    assert rows[3][3] == "Handled before accept_terms: click via preset cookies."
    assert "2 interruptions handled, 1 step skipped, 1 heal declined by the page guard." in \
        parsed.text


def test_a_dismissed_popup_is_a_flow_event_and_a_card(capsys, tmp_path):
    store = write_sample(tmp_path / "s", extra_events=[
        {"ts": "2026-10-04T11:00:04+00:00", "event": "flow.dismiss", "key": "email",
         "popup": '<div role="dialog" class="nl"> ("Get 10% off")', "how": "decline",
         "label": "No thanks", "tried": ["escape"]},
        {"ts": "2026-10-04T11:00:05+00:00", "event": "flow.dismiss", "key": "go",
         "popup": "<aside>", "how": "escape", "tried": []}])
    parsed = Page(page(capsys, "--store", str(store)))
    rows = parsed.tables["Flow events"]
    assert rows[0][3] == "Dismissed a popup in the way: <aside>, with Escape."
    assert rows[1][1:] == ["2026-10-04 11:00:04 UTC", "email",
                           'Dismissed a popup in the way: <div role="dialog" class="nl"> '
                           '("Get 10% off"), with its button No thanks.']
    assert cards(page(capsys, "--store", str(store)))["popups dismissed"] == "2"
    assert ("2 interruptions handled, 1 step skipped, 1 heal declined by the page guard, "
            "2 popups dismissed.") in parsed.text


def test_quarantines_come_from_the_store_and_the_journal(capsys, tmp_path):
    store = write_sample(tmp_path / "s", extra_events=[
        {"ts": "2026-10-05T00:00:00+00:00", "event": "heal.quarantine", "key": "gone",
         "xpath": "//b[@id='gone']"}])
    rows = Page(page(capsys, "--store", str(store))).tables["Quarantined selectors"]
    assert rows == [
        ["accept_terms", "//input[@name='terms']", "heal", "2026-10-02 09:30:01 UTC", "in the store"],
        ["gone", "//b[@id='gone']", "-", "2026-10-05 00:00:00 UTC", "journal only"]]


def test_a_quarantine_the_journal_does_not_know_says_so(capsys, tmp_path):
    store = write_sample(tmp_path / "s", journal=False)
    rows = Page(page(capsys, "--store", str(store))).tables["Quarantined selectors"]
    assert rows == [["accept_terms", "//input[@name='terms']", "heal", "not in the journal",
                     "in the store"]]


def test_without_a_journal_the_page_says_so_and_still_lists_the_keys(capsys, tmp_path):
    store = write_sample(tmp_path / "s", journal=False)
    out = page(capsys, "--store", str(store))
    text = Page(out).text
    assert "No journal.jsonl next to the store" in text
    assert "There is no journal.jsonl next to the store, so there are no heals to list." in text
    assert "so there are no flow events to list." in text
    counts = cards(out)
    assert counts["keys"] == "3" and counts["heals applied"] == "-"
    assert "Keys and their selectors" in Page(out).tables


def test_an_empty_store_and_an_empty_journal(capsys, tmp_path):
    folder = tmp_path / "s"
    folder.mkdir()
    store = folder / "overrides.json"
    store.write_text('{"schema": 2, "selectors": {}}', encoding="utf-8")
    (folder / "journal.jsonl").write_text("", encoding="utf-8")
    text = Page(page(capsys, "--store", str(store))).text
    assert "The store has no keys yet." in text
    assert "No heal has been applied yet." in text
    assert "No interruptions, skipped steps or page guard stops are recorded." in text
    assert "No selector is quarantined." in text


def test_long_lists_are_cut_to_the_latest(capsys, tmp_path):
    many = []
    for i in range(30):
        many.append({"ts": "2026-11-%02dT00:00:00+00:00" % (i + 1), "event": "heal.applied",
                     "key": "k%d" % i, "xpath": "//b[@id='n%d']" % i, "old": ["#o"]})
    for i in range(60):
        many.append({"ts": "2026-12-%02dT00:%02d:00+00:00" % (i % 28 + 1, i), "event": "flow.skip",
                     "key": "s%d" % i, "reason": "optional"})
    store = write_sample(tmp_path / "s", extra_events=many)
    parsed = Page(page(capsys, "--store", str(store)))
    heals = parsed.tables["Recent applied heals"]
    assert len(heals) == report_html.LAST_HEALS
    assert heals[0][1] == "k29"
    assert "Newest first, the last 25 of 33." in parsed.text
    flow = parsed.tables["Flow events"]
    assert len(flow) == report_html.LAST_FLOW
    assert "the last 50 of 64." in parsed.text
    assert "60 steps skipped" not in parsed.text and "61 steps skipped" in parsed.text


def test_every_cell_has_a_label_so_the_table_can_stack_on_a_phone(capsys, sample):
    out = page(capsys, "--store", str(sample))
    assert out.count("<td ") == out.count("<td data-label=")
    labels = set(re.findall(r'<td data-label="([^"]+)"', out))
    assert {"Key", "Role", "Selectors", "Heals", "When", "Selector change", "State",
            "Provider and model", "Latency", "Event", "What happened", "Selector", "Source",
            "Quarantined"} <= labels


def test_the_same_input_gives_the_same_bytes(capsys, sample):
    assert page(capsys, "--store", str(sample)) == page(capsys, "--store", str(sample))


# --- escaping ----------------------------------------------------------------------------

HOSTILE = '"><img src=x onerror=alert(1)>'
SCRIPT = "//a[@x=\"<script>alert('x')</script>\"] | //b[@y='&amp;']"


def hostile_store(tmp_path):
    folder = tmp_path / "evil"
    folder.mkdir()
    store = folder / "overrides.json"
    store.write_text(json.dumps({"schema": 2, "selectors": {
        HOSTILE: {"expectedRole": "<b>button</b>", "destructive": True, "candidates": [
            {"xpath": SCRIPT, "source": "<i>code</i>", "status": "<u>active</u>",
             "lastSucceededAt": "<s>never</s>", "frameXPath": "//iframe[@id='</td><td>']"},
            {"xpath": "x\x00y\x1b[31mz", "source": "heal", "status": "quarantined"},
        ]},
    }}), encoding="utf-8")
    events = [
        {"ts": "<ts>", "event": "heal.applied", "key": HOSTILE, "provider": "<b>Evil</b>",
         "model": "m\"'><svg onload=alert(2)>", "latency_ms": "<i>5</i>", "xpath": SCRIPT,
         "old": [HOSTILE, "</code><script>1</script>"]},
        {"ts": "2026-10-04T11:00:00+00:00", "event": "flow.interrupt", "key": "<k>",
         "before": "<svg/onload=alert(3)>", "action": "<a href=javascript:alert(4)>",
         "preset": "<p>"},
        {"ts": "2026-10-04T11:00:01+00:00", "event": "flow.skip", "key": HOSTILE,
         "reason": "</td><script>alert(5)</script>"},
        {"ts": "2026-10-04T11:00:02+00:00", "event": "flow.page_guard", "key": "k",
         "last_page": "/<a>", "page": "/?x=<b>"},
        {"ts": "2026-10-04T11:00:03+00:00", "event": "heal.quarantine", "key": HOSTILE,
         "xpath": "<q>"},
    ]
    (folder / "journal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events),
                                          encoding="utf-8")
    return store


def test_every_value_from_the_store_and_journal_is_escaped(capsys, tmp_path):
    out = page(capsys, "--store", str(hostile_store(tmp_path)))
    parsed = Page(out)
    for tag in ("img", "script", "svg", "b", "i", "u", "s", "a", "q", "k"):
        assert tag not in parsed.tags, tag
    assert "<img" not in out and "<script" not in out and "<svg" not in out
    text = parsed.text
    for shown in (HOSTILE, SCRIPT, "<b>Evil</b>", "<svg/onload=alert(3)>", "/?x=<b>",
                  "</td><script>alert(5)</script>", "<b>button</b>", "//iframe[@id='</td><td>']"):
        assert shown in text, shown
    assert set(parsed.tags) <= {
        "html", "head", "meta", "title", "style", "body", "div", "header", "h1", "p", "code",
        "main", "section", "h2", "table", "caption", "thead", "tr", "th", "tbody", "td", "span",
        "ul", "li", "footer"}


def test_control_characters_and_lone_surrogates_cannot_break_the_page(capsys, tmp_path):
    folder = tmp_path / "ctl"
    folder.mkdir()
    store = folder / "overrides.json"
    store.write_text('{"schema": 2, "selectors": {"k\\u0000\\u001b": {"expectedRole": "button", '
                     '"candidates": [{"xpath": "a\\ud800b\\u0007c", "source": "code", '
                     '"status": "active"}]}}}', encoding="utf-8")
    out = page(capsys, "--store", str(store))
    assert out.isascii()
    assert "\x00" not in out and "\x1b" not in out and "\x07" not in out
    assert "&#65533;" in out
    assert "a" in Page(out).text


def test_non_ascii_text_is_written_as_character_references(capsys, tmp_path):
    folder = tmp_path / "uni"
    folder.mkdir()
    store = folder / "overrides.json"
    store.write_text(json.dumps({"schema": 2, "selectors": {"ключ é \U0001f600": {
        "expectedRole": "button", "candidates": [
            {"xpath": "//button[.='Über']", "source": "code", "status": "active"}]}}}),
        encoding="utf-8")
    out = page(capsys, "--store", str(store))
    assert out.isascii()
    text = Page(out).text
    assert "ключ é \U0001f600" in text
    assert "//button[.='Über']" in text


def test_h_escapes_quotes_and_angle_brackets():
    assert report_html.h('<a href="x">\'&') == "&lt;a href=&quot;x&quot;&gt;&#x27;&amp;"
    assert report_html.h(None) == "None"
    assert report_html.h("a\x00b") == "a�b"


# --- what the journal confirms -----------------------------------------------------------

def test_a_heal_is_confirmed_by_a_canary_failed_by_a_quarantine_and_otherwise_open():
    events = [
        {"event": "heal.applied", "key": "a", "xpath": "x1", "ts": "t1"},
        {"event": "heal.canary", "key": "a", "xpath": "x1", "success": True},
        {"event": "heal.applied", "key": "b", "xpath": "x2", "ts": "t2"},
        {"event": "heal.canary", "key": "b", "xpath": "x2", "success": False},
        {"event": "heal.applied", "key": "c", "xpath": "x3", "ts": "t3"},
        {"event": "heal.quarantine", "key": "c", "xpath": "x3"},
        {"event": "heal.applied", "key": "d", "xpath": "x4", "ts": "t4"},
        {"event": "heal.canary", "key": "other", "xpath": "x4", "success": True},
        {"event": "heal.applied", "key": "a", "xpath": "x1", "ts": "t5"},    # healed again
    ]
    assert [(r["key"], r["state"]) for r in report_html._heal_records(events)] == [
        ("a", "confirmed"), ("b", "failed"), ("c", "failed"), ("d", "unconfirmed"),
        ("a", "unconfirmed")]


def test_latency_is_shown_in_ms_or_seconds():
    assert [report_html._latency(v) for v in (0, 650, 999, 1000, 1400, 12345, None, "x", True)] == [
        "0 ms", "650 ms", "999 ms", "1.0 s", "1.4 s", "12.3 s", "-", "-", "-"]


# --- --out and the other formats ---------------------------------------------------------

def test_out_writes_the_file_and_says_where(capsys, sample, tmp_path):
    target = tmp_path / "reports" / "nested" / "report.html"
    assert cli.main(["report", "--format", "html", "--store", str(sample), "--out", str(target)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Wrote " + str(target) + "\n"
    assert captured.err == ""
    written = target.read_bytes()
    assert written.isascii()
    assert written.decode("ascii") == page(capsys, "--store", str(sample))
    assert [p.name for p in target.parent.iterdir()] == ["report.html"]


def test_out_replaces_an_existing_file(capsys, sample, tmp_path):
    target = tmp_path / "report.html"
    target.write_text("old", encoding="utf-8")
    assert cli.main(["report", "--format", "html", "--store", str(sample), "--out", str(target)]) == 0
    assert target.read_text(encoding="ascii").startswith("<!doctype html>")


def test_out_works_for_the_other_formats(capsys, sample, tmp_path):
    for fmt, name in (("md", "r.md"), ("json", "r.json"), ("text", "r.txt")):
        assert cli.main(["report", "--format", fmt, "--store", str(sample)]) == 0
        printed = capsys.readouterr().out
        target = tmp_path / name
        assert cli.main(["report", "--format", fmt, "--store", str(sample), "--out", str(target)]) == 0
        capsys.readouterr()
        assert target.read_text(encoding="utf-8").replace("\r\n", "\n") == printed


def test_out_to_a_path_that_cannot_be_written_is_an_error(capsys, sample, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    code = cli.main(["report", "--format", "html", "--store", str(sample),
                     "--out", str(blocker / "report.html")])
    captured = capsys.readouterr()
    assert (code, captured.out) == (1, "")
    assert captured.err.startswith("mendwright report: could not write ")
    assert captured.err.endswith("). Check the folder and its permissions, or leave out --out to "
                                 "print the report instead.\n")


def test_html_needs_a_store_like_the_other_formats(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["report", "--format", "html"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("mendwright report: no store at ")


def test_html_is_a_report_format_and_is_in_the_help(capsys):
    assert cli.main(["report", "--help"]) == 0
    out = capsys.readouterr().out
    assert "{text,md,json,html}" in out
    assert "--out FILE" in out


def test_html_prints_nothing_but_the_page_on_stdout(capsys, sample):
    out = page(capsys, "--store", str(sample))
    assert out.endswith("</html>\n")
    assert out.count("<!doctype") == 1
