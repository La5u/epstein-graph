import sqlite3
import tempfile
import unittest
from pathlib import Path

import visualize_graph
from build_doc_info import type_label


class DocInfoTests(unittest.TestCase):
    def test_type_label(self):
        self.assertEqual(type_label('financial.bank_statement'), 'bank statement')
        self.assertEqual(type_label('court.filing'), 'court filing')
        self.assertEqual(type_label('memo'), 'memo')

    def test_subpoena_targets_match_people_and_organizations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'info.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE subpoenas(efta TEXT, target TEXT, category TEXT, date_issued TEXT, statutes TEXT, clauses INTEGER)')
                db.executemany('INSERT INTO subpoenas VALUES (?,?,?,?,?,?)', [
                    ('E1', 'Darian Keldry', 'Individual', 'Oct 1, 2019', '', 1),
                    ('E2', 'JPMC', 'Other Entity', '', '', 1),
                    ('E3', 'Deutsche Bank AG, New York Branch / Deutsche Bank Securities Inc', 'Financial Institution', '', '', 1)])
            entities = [{'id': 1, 'name': 'Darian K. Keldry', 'entity_type': 'person'},
                        {'id': 2, 'name': 'Darian K. Keldry PLLC', 'entity_type': 'organization'},
                        {'id': 3, 'name': 'JPMorgan Chase', 'entity_type': 'organization'},
                        {'id': 4, 'name': 'Deutsche Bank Securities Inc.', 'entity_type': 'organization'},
                        {'id': 5, 'name': 'Darian Keldry', 'entity_type': 'survivor'}]
            tagged = {e['id']: [x['efta_number'] for x in e.get('subpoenas', [])] for e in visualize_graph.with_subpoenas(entities, path)}
        self.assertEqual(tagged, {1: ['E1'], 2: [], 3: ['E2'], 4: ['E3'], 5: []})


if __name__ == '__main__':
    unittest.main()
