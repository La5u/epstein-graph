import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import networkx as nx

import export_neo4j


def rows(path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


class ExportTests(unittest.TestCase):
    def test_csvs_graphml_and_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'output'
            out.mkdir()
            (out / 'graph_entities.json').write_text(json.dumps([
                {'id': 1, 'name': 'Jane Roe', 'entity_type': 'person', 'legal_status_flags': ['convicted'], 'original_names': ['Jane Roe', 'J. Roe']},
                {'id': 'place_x', 'name': 'X Airport', 'entity_type': 'location', 'place_type': 'airport'},
                {'id': 'email_ab', 'name': 'Email_ab', 'entity_type': 'email'}]))
            (out / 'graph_relationships.json').write_text(json.dumps([
                {'id': 7, 'source_entity_id': 1, 'target_entity_id': 'place_x', 'relationship_type': 'flew_to_or_from', 'weight': 2,
                 'date_first': '2001-01-01', 'date_last': None},
                {'id': 8, 'source_entity_id': 1, 'target_entity_id': 'gone', 'relationship_type': 'sent_to', 'weight': 1}]))
            (out / 'identity_map.json').write_text(json.dumps({'email_cd': {'canonical': 1, 'rule': 'reviewed_identity_merge', 'confidence': 'confirmed'}}))
            with patch.object(export_neo4j, 'OUT', out):
                result = export_neo4j.export(out_dir=Path(tmp) / 'neo4j')
            target = Path(tmp) / 'neo4j'
            nodes = rows(target / 'nodes.csv')
            self.assertEqual(result, {'directory': str(target), 'nodes': 3, 'relationships': 1, 'aliases': 1})
            self.assertEqual(nodes[0]['id:ID'], 'kg:1')  # graph entity IDs as kg:<n>
            self.assertEqual(nodes[0]['legal_status_flags:string[]'], 'convicted')
            self.assertEqual(nodes[1][':LABEL'], 'Entity;Location;Airport')
            rels = rows(target / 'relationships.csv')
            self.assertEqual([(r[':START_ID'], r[':END_ID'], r[':TYPE']) for r in rels], [('kg:1', 'place_x', 'FLEW_TO_OR_FROM')])  # dangling edge dropped
            alias = rows(target / 'alias_of.csv')[0]
            self.assertEqual((alias[':START_ID'], alias[':END_ID'], alias['rule']), ('email_cd', 'kg:1', 'reviewed_identity_merge'))
            self.assertEqual(nx.read_graphml(target / 'graph.graphml').number_of_edges(), 1)


if __name__ == '__main__':
    unittest.main()
