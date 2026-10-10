"""Unit coverage for the self-healing safety gate, store, capture and client apply."""

from mendwright import (
    SelectorStore,
    apply,
    build_bundle,
    merge,
    migrate_schema1,
    redact,  # noqa: F401  (part of the public API this file imports)
    validate,
)

CHECKBOX_HTML = '<form><label><input type="checkbox" id="tc"> Accept terms</label></form>'
TWO_CHECKBOXES = ('<form><input type="checkbox" id="a">'
                  '<input type="checkbox" id="b"></form>')


# --- validation gate --------------------------------------------------------
def test_validate_accepts_unique_correct_role():
    r = validate("//input[@type='checkbox']", CHECKBOX_HTML, expected_role='checkbox')
    assert r.ok and r.count == 1


def test_validate_rejects_non_unique():
    r = validate("//input[@type='checkbox']", TWO_CHECKBOXES, expected_role='checkbox')
    assert not r.ok and r.count == 2


def test_validate_rejects_role_mismatch():
    r = validate("//button", '<button>x</button>', expected_role='checkbox')
    assert not r.ok and 'role mismatch' in r.reason


def test_validate_rejects_brittle_absolute_path():
    r = validate('/html[1]/body[1]/div[3]/input[1]', CHECKBOX_HTML, expected_role='checkbox')
    assert not r.ok and 'brittle' in r.reason


def test_validate_destructive_requires_exact_text():
    html = '<button>Registrieren</button>'
    ok = validate('//button', html, expected_role='button',
                  expected_text=['Register', 'Registrieren'], destructive=True)
    bad = validate('//button', html, expected_role='button',
                   expected_text=['Pay now'], destructive=True)
    assert ok.ok and not bad.ok


# --- store ------------------------------------------------------------------
def test_merge_unions_candidates_by_xpath():
    seed = {'selectors': {'k': {'candidates': [{'xpath': 'a', 'status': 'active'}]}}}
    override = {'selectors': {'k': {'candidates': [{'xpath': 'b', 'status': 'active'}]}}}
    out = merge(seed, override)
    xpaths = {c['xpath'] for c in out['selectors']['k']['candidates']}
    assert xpaths == {'a', 'b'}


def test_quarantine_hides_candidate(tmp_path):
    data = {'schema': 2, 'selectors': {'k': {'expectedRole': 'button', 'candidates': [
        {'xpath': 'a', 'status': 'active'}, {'xpath': 'b', 'status': 'active'}]}}}
    store = SelectorStore(data, str(tmp_path / 'o.json'))
    store.quarantine('k', 'a')
    assert [c['xpath'] for c in store.candidates('k')] == ['b']


def test_migrate_schema1_marks_injected_destructive_keys():
    data = {'selectors': [{'key': 'pay', 'xpath': "//button[@id='p']"}]}
    out = migrate_schema1(data, destructive_keys=frozenset({'pay'}))
    assert out['selectors']['pay']['destructive'] is True


# --- capture redaction ------------------------------------------------------
def test_build_bundle_redacts_values_and_secrets():
    html = '<input value="SECRET-PW"><a href="https://x.com/u">x</a>'
    row = {'secrets': {'password': 'SECRET-PW'}}
    bundle = build_bundle('k', {'expectedRole': 'input'}, [], [], html,
                          {'url': 'https://x', 'stage': 's', 'version': '1'}, row)
    assert 'SECRET-PW' not in bundle['html']
    assert 'https://x.com/u' not in bundle['html']  # URL scrubbed


# --- client apply -----------------------------------------------------------
def test_apply_adds_first_valid_candidate(tmp_path):
    data = {'schema': 2, 'selectors': {'terms': {'expectedRole': 'checkbox',
            'candidates': [{'xpath': 'stale', 'status': 'active'}]}}}
    store = SelectorStore(data, str(tmp_path / 'o.json'))
    proposal = {'xpath': '//bad[', 'fallbacks': ["//input[@type='checkbox']"]}
    applied, reason = apply(store, 'terms', proposal, CHECKBOX_HTML, store.meta('terms'))
    assert applied == "//input[@type='checkbox']"  # invalid primary skipped, fallback applied
    assert reason == 'ok'
