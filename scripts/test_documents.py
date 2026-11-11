"""Small synthetic fixtures only: no corpus or rebuild required."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_documents as documents
import corpus as corpus_module
from adapters import efta as efta_adapter


class DocumentIndexTests(unittest.TestCase):
    def test_keep_supersets_drops_copies_only_beside_their_superset(self):
        canonical_of = {'EFTA00000002': 'EFTA00000001', 'EFTA00000003': 'EFTA00000001'}
        lists = {'e1': {'EFTA00000001': None, 'EFTA00000002': None, 'EFTA00000004': None},
                 'e2': {'EFTA00000002': None, 'EFTA00000003': None}}  # superset not in this list: both stay
        self.assertEqual(documents.keep_supersets(lists, canonical_of), 1)
        self.assertEqual(sorted(lists['e1']), ['EFTA00000001', 'EFTA00000004'])
        self.assertEqual(sorted(lists['e2']), ['EFTA00000002', 'EFTA00000003'])

    def test_cited_documents_walks_strings_lists_and_json(self):
        metadata = '{"efta": "EFTA00000001", "notes": "Source: EFTA00000002", "sample_documents": [{"efta_number": "EFTA00000003"}]}'
        self.assertEqual(sorted(corpus_module.cited_documents(metadata)), ['EFTA00000001', 'EFTA00000002', 'EFTA00000003'])
        self.assertEqual(corpus_module.cited_documents(None), [])

    def test_cited_inner_pages_map_to_their_document(self):
        corpus = sqlite3.connect(':memory:')
        corpus.execute('CREATE TABLE documents(efta_number, total_pages)')
        corpus.executemany('INSERT INTO documents VALUES (?, ?)', [('EFTA01660622', 21), ('EFTA01660643', 2)])
        locate = corpus_module.page_locator(corpus)
        self.assertEqual(locate('EFTA01660636'), ('EFTA01660622', 14))
        self.assertEqual(locate('EFTA01660622'), ('EFTA01660622', 0))
        self.assertEqual(locate('EFTA01660700'), ('EFTA01660700', None))  # past the last document's pages
        corpus.close()

    def test_search_names_skip_pseudonyms_and_unknowns(self):
        self.assertEqual(documents.search_names({'id': 'email_x', 'name': 'Email_x'}, {'email_x': 'a@b.com'}), ['a b com'])
        self.assertEqual(documents.search_names({'id': 3, 'name': 'Andy ?'}, {}), [])
        self.assertEqual(documents.search_names({'id': 4, 'name': 'Leon Black', 'original_names': ['LEON BLACK']}, {}),
                         ['Leon Black', 'LEON BLACK'])

    def test_links_get_cited_flight_and_fallback_documents(self):
        corpus = sqlite3.connect(':memory:')
        corpus.execute('CREATE TABLE pages(id INTEGER PRIMARY KEY, efta_number, page_number, text_content)')
        corpus.execute('CREATE VIRTUAL TABLE pages_fts USING fts5(text_content)')
        for i, (efta, text) in enumerate([('EFTA00000010', 'Ann Smith met Bob Jones'), ('EFTA00000011', 'Ann Smith alone')], 1):
            corpus.execute('INSERT INTO pages VALUES (?, ?, 0, ?)', (i, efta, text))
            corpus.execute('INSERT INTO pages_fts(rowid, text_content) VALUES (?, ?)', (i, text))
        efta_adapter.install_views(corpus)
        entities = [{'id': 1, 'name': 'Ann Smith', 'entity_type': 'person'}, {'id': 2, 'name': 'Bob Jones', 'entity_type': 'person'},
                    {'id': 3, 'name': 'Cat Lee', 'entity_type': 'person'}]
        relationships = [
            {'id': 'cited', 'source_entity_id': 1, 'target_entity_id': 3, 'relationship_type': 'associated_with',
             'metadata': '{"efta": "EFTA00000099"}'},
            {'id': 'text', 'source_entity_id': 1, 'target_entity_id': 2, 'relationship_type': 'owned_by', 'metadata': {}},
            {'id': 'flew', 'source_entity_id': 2, 'target_entity_id': 3, 'relationship_type': 'traveled_with',
             'metadata': {'sample_dates': ['2001-01-01']}}]
        flights = {'f-1': {'date': '2001-01-01', 'origin': 'TEB', 'destination': 'PBI', 'people': [2], 'places': []}}
        with tempfile.TemporaryDirectory() as directory, patch.object(documents, 'OUT', Path(directory)):
            (Path(directory) / 'flight_index.json').write_text(__import__('json').dumps(flights))
            edge_docs, records, own, _, fallback = documents.build(entities, relationships, corpus)
        corpus.close()
        self.assertEqual(edge_docs['cited'], {'EFTA00000099': (None, 'cited in the record')})
        self.assertEqual(edge_docs['text'], {'EFTA00000010': (0, 'full text names both (fallback)')})
        self.assertEqual(records['flew'][0][2], documents.FLIGHT + 'f-1')  # same-date flight carrying one of them
        self.assertEqual(fallback, 1)
        self.assertEqual(own, {})  # every node is covered by a link


if __name__ == '__main__':
    unittest.main()
