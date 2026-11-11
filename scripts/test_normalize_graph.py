import json
import unittest
from pathlib import Path

from normalize_graph import final_target, normalize, name_key


class NormalizationTests(unittest.TestCase):
    def test_flight_log_artifacts_and_files_stay_as_hidden_evidence(self):
        labels = ['PBI West Palm Beach, FL, United States', 'TIST Water Island, St. Thomas, United',
                  '36653-G-1159B-N908JE-PBI-TIST-FOIA-1336-No Records ?',
                  'Passengers, No No Passengers', 'Passenger (0) Test Flight', '?, VELDON RRY VELDON RRY ?',
                  '36653-G-1159B-N908JE-PBI-TIST-1336', 'Passengers', 'Passenger 0 Test Flight']
        entities = [dict(id=9000+i, name=n, entity_type='person') for i, n in enumerate(labels)]
        entities += [dict(id=9999, name='Passenger Smith', entity_type='person'),
                     dict(id=9998, name='image003.jpg', entity_type='organization')]
        nodes, _, _ = normalize(entities, [], {})
        self.assertEqual(len(nodes), len(entities))
        for node in nodes[:len(labels)]:
            self.assertTrue(node['suspected_parse_artifact'])
            self.assertEqual(node['label_quality'], 'not_a_name')
            self.assertEqual(node['original_records'][0]['name'], node['name'])
        self.assertNotIn('suspected_parse_artifact', nodes[-2])
        self.assertEqual(nodes[-1]['label_quality_reason'], 'file_name')

    def test_case_whitespace_unicode(self):
        self.assertEqual(name_key('  JEFFREY\tEpstein '), 'jeffrey epstein')
        self.assertEqual(name_key('Jose\u0301'), name_key('JOSÉ'))

    def test_chained_reviewed_merges_use_final_target(self):
        item = {'confidence': 'reviewed', 'reason': 'test'}
        entities = [dict(id=i, name=n, entity_type='person') for i, n in ((1, 'Ann Alpha'), (2, 'Ann Beta'), (3, 'Ann Gamma'))]
        curated = {1: (2, item), 2: (3, item)}
        self.assertEqual(final_target(curated, 1), 3)
        nodes = normalize(entities, [], curated)[0]
        self.assertEqual([(n['id'], n['original_entity_ids']) for n in nodes], [(3, [1, 2, 3])])
        with self.assertRaises(ValueError):
            final_target({1: (2, item), 2: (1, item)}, 1)

    def test_scope_and_provenance(self):
        entities = [dict(id=1, name='Jeffrey Epstein', entity_type='person'),
                    dict(id=59, name='jeffrey EPSTEIN', entity_type='person'),
                    dict(id=3, name='Epstein', entity_type='person'),
                    dict(id=4, name='Jeffrey Epstein', entity_type='organization')]
        relationships = [dict(id=10, source_entity_id=59, target_entity_id=3, weight=9),
                         dict(id=11, source_entity_id=1, target_entity_id=3, weight=2)]
        nodes, edges, merges = normalize(entities, relationships)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0]['original_entity_ids'], [1, 4, 59])
        self.assertEqual(nodes[0]['original_records'], [entities[0], entities[3], entities[1]])
        self.assertEqual(edges[0]['source_entity_id'], 1)
        self.assertEqual(edges[0]['original_source_entity_id'], 59)
        self.assertEqual([e['weight'] for e in edges], [9, 2])
        self.assertEqual(relationships[0]['source_entity_id'], 59)
        self.assertEqual(len(merges), 1)

    def test_real_corpus(self):
        raw = Path(__file__).resolve().parents[1] / 'data/raw'
        entities = json.loads((raw / 'knowledge_graph_entities.json').read_text())
        relationships = json.loads((raw / 'knowledge_graph_relationships.json').read_text())
        nodes, edges, _ = normalize(entities, relationships)
        matches = [n for n in nodes if n['entity_type'] == 'person' and n['normalized_name'] == 'jeffrey epstein']
        self.assertEqual(len(matches), 1)
        self.assertIn(59, matches[0]['original_entity_ids'])
        self.assertEqual(len(edges), len(relationships))
        self.assertEqual({e['id'] for e in edges}, {e['id'] for e in relationships})
        self.assertTrue(all(e['source_entity_id'] != 59 and e['target_entity_id'] != 59 for e in edges))
        self.assertEqual(len({(n['entity_type'], n['normalized_name']) for n in nodes}), len(nodes))
        self.assertEqual(sum(len(n['original_records']) for n in nodes), len(entities))


if __name__ == '__main__':
    unittest.main()
