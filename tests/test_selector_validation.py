import unittest
from pathlib import Path

from mendwright import validate

FIX = Path(__file__).parent / 'fixtures'

TERMS = """<html><body><main><form>
  <dml-checkbox><label><input type="checkbox">
    <span>I hereby accept the <a href="https://shop.example.com/en/terms-and-conditions">terms of use</a>.</span>
  </label></dml-checkbox>
  <dml-consent-checkbox><label><input type="checkbox">
    <span>newsletter <a href="https://shop.example.com/en/club/partner">partners</a></span>
  </label></dml-consent-checkbox>
</form></main></body></html>"""

PAY = """<html><body><main>
  <button type="submit"><span>Zahlungspflichtig bestellen</span></button>
  <button>Edit</button>
</main></body></html>"""

TERMS_XPATH = "//main//label[.//a[contains(@href,'terms-and-conditions')]]//input[@type='checkbox']"


class ValidationTests(unittest.TestCase):
    def test_exactly_one_checkbox_passes(self):
        r = validate(TERMS_XPATH, TERMS, expected_role='checkbox')
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.count, 1)

    def test_no_match_fails(self):
        r = validate("//main//input[@id='nope']", TERMS, expected_role='checkbox')
        self.assertFalse(r.ok)
        self.assertEqual(r.count, 0)

    def test_not_unique_fails(self):
        r = validate("//main//input[@type='checkbox']", TERMS, expected_role='checkbox')
        self.assertFalse(r.ok)
        self.assertEqual(r.count, 2)

    def test_role_mismatch_fails(self):
        r = validate("//main//a[contains(@href,'terms-and-conditions')]", TERMS, expected_role='checkbox')
        self.assertFalse(r.ok)

    def test_destructive_exact_action_text_passes(self):
        r = validate("//main//button[@type='submit']", PAY, expected_role='button',
                     expected_text=['Pay', 'Zahlungspflichtig bestellen'], destructive=True)
        self.assertTrue(r.ok, r.reason)

    def test_destructive_wrong_action_text_rejected(self):
        r = validate("//main//button[not(@type)]", PAY, expected_role='button',
                     expected_text=['Pay', 'Zahlungspflichtig bestellen'], destructive=True)
        self.assertFalse(r.ok)

    def test_brittle_absolute_path_rejected(self):
        r = validate("/html[1]/body[1]/main[1]/div[3]/input[1]", TERMS, expected_role='checkbox')
        self.assertFalse(r.ok)
        self.assertIn('brittle', r.reason.lower())

    def test_terms_moved_fixture_validates(self):
        html = (FIX / 'terms_moved.html').read_text(encoding='utf-8')
        r = validate(TERMS_XPATH, html, expected_role='checkbox')
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.count, 1)


if __name__ == '__main__':
    unittest.main()
