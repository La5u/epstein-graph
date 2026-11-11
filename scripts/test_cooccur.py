"""Small FTS5 fixtures; no corpus or rebuild required."""
import contextlib
import io
import sqlite3
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import cooccur
import corpus


class CooccurTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / 'corpus.db'
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE documents(efta_number TEXT PRIMARY KEY, dataset TEXT,
                                   doc_type TEXT, pdf_url TEXT);
            CREATE TABLE document_classification(efta_number TEXT PRIMARY KEY, doc_type TEXT);
            CREATE TABLE pages(id INTEGER PRIMARY KEY, efta_number TEXT,
                               page_number INTEGER, text_content TEXT);
            CREATE VIRTUAL TABLE pages_fts USING fts5(
                efta_number UNINDEXED, page_number UNINDEXED, text_content);
        ''')
        self.db.executemany('INSERT INTO documents VALUES (?, ?, ?, ?)', [
            ('A', '1', 'letter', 'https://example.org/a.pdf'),
            ('B', '2', 'email', None),
        ])
        self.db.executemany('INSERT INTO document_classification VALUES (?, ?)', [('A', 'letter'), ('B', 'email')])
        self.db.execute("UPDATE documents SET doc_type='legacy'")
        pages = [
            (1, 'A', 1, 'Before LEON BLACK after.'),
            (2, 'A', 3, 'A note about Jes Staley here.'),
            (3, 'A', 4, 'Leon Black again.'),
            (4, 'B', 2, 'Leon Black and Jes Staley together.'),
            (5, 'C', 1, 'Leon very Black and Jes Staley.'),
            (6, 'D', 1, 'Leon Black only.'),
            (7, 'E', 1, 'Jes Staley only.'),
            (8, 'F', 1, 'Leon'),
            (9, 'F', 2, 'Black and Jes Staley.'),
            (10, 'G', 7, 'Leon Black and Jes Staley without metadata.'),
        ]
        self.db.executemany('INSERT INTO pages VALUES (?, ?, ?, ?)', pages)
        self.db.executemany('INSERT INTO pages_fts(rowid, efta_number, page_number, text_content) VALUES (?, ?, ?, ?)', pages)
        self.db.commit()
        self.db.close()
        self.db = corpus.connect(path)
        self.addCleanup(self.db.close)
        self.phrases = ['Leon Black', 'Jes Staley']

    def test_document_intersection_and_provenance(self):
        total, rows = cooccur.find_documents(self.db, self.phrases, 20)
        self.assertEqual(total, 3)
        self.assertEqual([r[0] for r in rows], ['A', 'B', 'G'])
        self.assertEqual(rows[0][1:4], ('1', 'letter', 'https://example.org/a.pdf'))
        evidence = rows[0][4]
        self.assertEqual([(text, page) for text, page, _ in evidence], [
            ('Leon Black', 1), ('Leon Black', 4), ('Jes Staley', 3)])
        self.assertIn('[LEON BLACK]', evidence[0][2])
        self.assertIn('[Jes Staley]', evidence[2][2])
        self.assertEqual(rows[2][1:4], (None, None, None))

    def test_limit_counts_documents_not_pages(self):
        total, rows = cooccur.find_documents(self.db, self.phrases, 1)
        self.assertEqual(total, 3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0][4]), 3)
        self.assertEqual(cooccur.find_documents(self.db, self.phrases, 0), (3, []))

    def test_no_hits_single_and_duplicate_phrases(self):
        self.assertEqual(cooccur.find_documents(self.db, ['Absent Person'], 20), (0, []))
        total, _ = cooccur.find_documents(self.db, ['Leon Black'], 20)
        self.assertEqual(total, 4)
        total, _ = cooccur.find_documents(self.db, self.phrases * 2, 20)
        self.assertEqual(total, 3)

    def test_phrase_safety(self):
        self.assertEqual(cooccur.phrase('Leon-"Black"'), '"Leon Black"')
        with self.assertRaises(SystemExit):
            cooccur.find_documents(self.db, ['!!!'], 20)

    def run_main(self, *args):
        output = io.StringIO()
        # main closes its connection; preserve the fixture for addCleanup.
        connection = unittest.mock.Mock(wraps=self.db)
        connection.close.side_effect = lambda: None
        with patch.object(sys, 'argv', ['cooccur.py', *args]), \
                patch.object(cooccur.corpus, 'connect', return_value=connection), \
                patch.object(cooccur, 'document_urls', return_value={'G': 'https://example.org/g.pdf'}), \
                contextlib.redirect_stdout(output):
            cooccur.main()
        return output.getvalue()

    def test_document_cli(self):
        output = self.run_main(*self.phrases, '--document')
        self.assertIn('3 matching documents', output)
        self.assertIn("'Jes Staley' p.3:", output)
        self.assertIn('https://example.org/a.pdf', output)
        self.assertIn('https://example.org/g.pdf', output)

    def test_page_default_and_near_unchanged(self):
        output = self.run_main(*self.phrases)
        self.assertIn('2 matching pages', output)
        self.assertNotIn('A p.', output)
        output = self.run_main(*self.phrases, '--near', '1')
        self.assertIn('2 matching pages', output)
        self.assertEqual(cooccur.build_query(self.phrases, 0),
                         'text_content:"Leon Black" AND text_content:"Jes Staley"')

    def test_document_rejects_any_explicit_near_before_opening_database(self):
        for near in ['0', '10', '-1']:
            with self.subTest(near=near), \
                    patch.object(sys, 'argv', ['cooccur.py', 'Name', '--document', '--near', near]), \
                    patch.object(cooccur.corpus, 'connect') as connect, \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    cooccur.main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn('--near cannot be used with --document', error.getvalue())
                connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
