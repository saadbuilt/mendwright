import unittest

from mendwright import build_bundle, redact

ROW = {
    'secrets': {'password': 'Hunter2secret'},
    'proxy': {'password': 'proxypass9'},
    'profile': {'first_name': 'Jane', 'email': 'jane.doe@example.com'},
}
META = {'expectedRole': 'checkbox', 'expectedText': ['terms of use'], 'destructive': False}


class RedactTests(unittest.TestCase):
    def test_redacts_row_password(self):
        out = redact('<span>pw=Hunter2secret</span>', ROW)
        self.assertNotIn('Hunter2secret', out)
        self.assertIn('[redacted]', out)

    def test_redacts_profile_value(self):
        self.assertNotIn('jane.doe@example.com', redact('<a>jane.doe@example.com</a>', ROW))

    def test_strips_input_values(self):
        out = redact('<input type="text" value="Tim Neumann"><input type="checkbox">', ROW)
        self.assertNotIn('value="Tim Neumann"', out)
        self.assertIn('type="checkbox"', out)  # structure retained


class BundleTests(unittest.TestCase):
    def test_bundle_fields_and_redaction(self):
        html = '<main><input value="Tim"><a>Hunter2secret</a></main>'
        inv = [{'text': 'Hunter2secret', 'xpath': "//a"}]
        b = build_bundle('terms', META, [{'xpath': '//old'}], inv, html,
                         {'url': 'https://x', 'stage': 'reg', 'version': 'build-24'}, row=ROW)
        self.assertEqual(b['key'], 'terms')
        self.assertEqual(b['expectedRole'], 'checkbox')
        self.assertEqual(b['candidates'], ['//old'])
        self.assertNotIn('Hunter2secret', b['html'])
        self.assertNotIn('value="Tim"', b['html'])
        self.assertNotIn('Hunter2secret', str(b['inventory']))
        self.assertEqual(b['stage'], 'reg')

    def test_destructive_meta_carried(self):
        b = build_bundle('submit_payment',
                         {'expectedRole': 'button', 'expectedText': ['Pay', 'Zahlungspflichtig bestellen'],
                          'destructive': True},
                         [], [], '<main></main>', {}, row=None)
        self.assertTrue(b['destructive'])
        self.assertIn('Zahlungspflichtig bestellen', b['expectedText'])


if __name__ == '__main__':
    unittest.main()
