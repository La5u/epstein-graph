import json
from pathlib import Path
import tempfile
import unittest

from involvement import enrichment, flight_evidence, iso_date, redaction_stats
from survivor_policy import survivor_ids


class InvolvementTests(unittest.TestCase):
    def test_shared_survivor_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'survivors.json'
            config.write_text(json.dumps({'names': ['Configured Name']}))
            entities = [
                dict(id=1, name=' configured  NAME '),
                dict(id=2, name='Labelled Name', metadata='{"person_type":"victim"}'),
                dict(id=3, name='Relationship Name'),
                dict(id=4, name='Other Name', metadata='invalid'),
                dict(id=5, name='Canonical Name', original_names=['Configured Name']),
            ]
            relationships = [dict(source_entity_id=3, target_entity_id=4, relationship_type='victim_of')]
            self.assertEqual(survivor_ids(entities, relationships, config), {1, 2, 3, 5})

    def test_missing_survivor_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                survivor_ids([], config=Path(directory) / 'missing.json')

    def test_unicode_survivor_names(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'survivors.json'
            config.write_text(json.dumps({'names': ['René Example']}))
            self.assertEqual(survivor_ids([dict(id=1, name='Rene\u0301 Example')], config=config), {1})

    def test_enrichment_missing_and_observed_denominators(self):
        self.assertEqual(enrichment(2, 4, 10, 100), 5)
        self.assertIsNone(enrichment(0, 0, 10, 100))
        self.assertIsNone(enrichment(0, 4, 0, 100))
        self.assertEqual(enrichment(0, 4, 10, 100), 0)

    def test_redactions_missing_is_not_zero(self):
        self.assertEqual(redaction_stats({'a', 'b'}, {'a': 4, 'b': None, 'outside': 999}, {'a', 'b'}), (1, 4, 1))
        self.assertEqual(redaction_stats({'a'}, {}, {'a'}), (0, None, None))
        self.assertEqual(redaction_stats({'a'}, {'a': 0}, {'a'}), (1, 0, None))

    def test_date_specific_flight_provenance(self):
        relationship = dict(id=7, metadata={'sample_dates': ['2000-01-01', '2000-01-02'],
            'flights': [{'date': '2000-01-01', 'flight_id': 12, 'efta_number': 'EFTA123'}]})
        evidence = list(flight_evidence(relationship))
        self.assertEqual(len(evidence), 2)
        self.assertEqual(json.loads(evidence[0][1])['flight']['flight_id'], 12)
        self.assertEqual(json.loads(evidence[1][1])['relationship_id'], 7)
        self.assertIn('unavailable', json.loads(evidence[1][1])['provenance'])
        self.assertEqual(list(flight_evidence(dict(id=8, metadata={}))), [])

    def test_impossible_calendar_dates_are_dropped(self):
        self.assertTrue(iso_date('2000-02-29'))
        for value in ('2010-02-30', '2010-13-01', '2010-1-01', None, 20100101):
            self.assertFalse(iso_date(value))
        relationship = dict(id=9, metadata={'sample_dates': ['2010-02-30'], 'flights': [{'date': '2010-02-31'}]})
        self.assertEqual(list(flight_evidence(relationship)), [])


if __name__ == '__main__':
    unittest.main()
