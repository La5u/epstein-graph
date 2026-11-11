import json
import unittest

import audit_coverage as audit


class AuditRecipientTests(unittest.TestCase):
    def test_cached_field_summary_matches_original_per_row_transform(self):
        for raw in ('[]', '', None, 'null', '{}', 'bad JSON',
                    json.dumps(['Name <user@g', 'mail.com>', ['Other Name', 'other@example.test'], 123, None])):
            try:
                values = json.loads(raw or '[]')
                if not isinstance(values, list):
                    expected = None
                else:
                    expected = (sum(not isinstance(v, str) for v in values),
                                audit.join_split_addresses(pair for value in values for pair in audit.parse_recipient(value)))
            except (ValueError, TypeError):
                expected = None
            self.assertEqual(audit.recipient_fields(raw), expected)
