import unittest

from legal_status import annotate


class LegalStatusTests(unittest.TestCase):
    def test_only_explicit_person_statuses_create_flags(self):
        entities = [dict(id=1, entity_type='person', metadata='{"legal_status":"convicted"}'),
                    dict(id=2, entity_type='person', metadata='{"person_type":"perpetrator","legal_status":"no_action"}'),
                    dict(id=3, entity_type='person', metadata='{"legal_status":"unknown"}'),
                    dict(id=4, entity_type='organization', metadata='{"legal_status":"convicted"}'),
                    dict(id=5, entity_type='person', metadata='{"legal_status":"deceased"}')]
        counts = annotate(entities, [], 'https://example.test/export')
        self.assertEqual(counts, {'convicted': 1, 'in_custody': 0, 'under_investigation': 0, 'deceased': 1})
        self.assertEqual(entities[0]['legal_status_flags'], ['convicted'])
        self.assertFalse(entities[0]['legal_status_evidence'][0]['current_status_verified'])
        self.assertTrue(all(not e['legal_status_flags'] for e in entities[1:4]))
        self.assertEqual(entities[4]['legal_status_flags'], ['deceased'])

    def test_distinct_sourced_flags_and_validation(self):
        entities = [dict(id=1, entity_type='person', metadata='{"legal_status":"convicted"}')]
        source = dict(entity_id=1, status='in_custody', source_url='https://example.test/court', as_of='2020-01-01')
        counts = annotate(entities, [source], 'https://example.test/export')
        self.assertEqual(entities[0]['legal_status_flags'], ['convicted', 'in_custody'])
        self.assertEqual(counts['under_investigation'], 0)
        self.assertEqual(entities[0]['legal_status_evidence'][1]['as_of'], '2020-01-01')
        with self.assertRaises(ValueError):
            annotate(entities, [{**source, 'source_url': 'file:///tmp/unsupported'}], 'https://example.test/export')
        with self.assertRaises(ValueError):
            annotate(entities, [{**source, 'as_of': 'yesterday'}], 'https://example.test/export')


if __name__ == '__main__':
    unittest.main()
