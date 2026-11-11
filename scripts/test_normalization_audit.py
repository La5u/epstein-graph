import json
import unittest

from audit_normalization import summarize
from build_contacts import contact_id, normalize_contact


class NormalizationAuditTests(unittest.TestCase):
    def test_candidates_are_review_only_and_private(self):
        key = b'key'
        emails = ['Name@EXAMPLE.TEST', 'name@example.test', 'a@xn--bcher-kva.test', 'a@bücher.test']
        phones = ['+1 (202) 555-0100', '12025550100', '202-555-0100']
        result = summarize(emails, phones, key)
        self.assertEqual(len(result['email_local_case']), 1)
        self.assertEqual(len(result['email_idna_domain']), 1)
        # +/country-code spellings of one North American number normalize to one ID.
        self.assertEqual(result['phone_plus_prefix'], [])
        self.assertEqual(result['phone_us_prefix'], [])
        self.assertEqual(len({normalize_contact(p, 'phone') for p in phones}), 1)
        self.assertNotEqual(contact_id(normalize_contact(emails[0], 'email'), 'email', key),
                            contact_id(normalize_contact(emails[1], 'email'), 'email', key))
        serialized = json.dumps(result)
        for value in emails + phones:
            self.assertNotIn(value, serialized)


if __name__ == '__main__':
    unittest.main()
