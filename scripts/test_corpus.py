import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import corpus
from adapters import efta
from corpus_index import page_map


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'corpus.db'
        with closing(sqlite3.connect(self.path)) as db:
            efta.create(db, [('EFTA00000002', 0, 'Ann Smith met Bob Jones'), ('EFTA00000001', 0, 'cover'),
                             ('EFTA00000001', 1, 'Ann Smith alone')],
                        [('EFTA00000001', 2), ('EFTA00000002', 1)])

    def tearDown(self):
        self.dir.cleanup()

    def test_views_use_common_names(self):
        with closing(corpus.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT page_id, doc_id, page, text FROM pages ORDER BY page_id').fetchall(),
                             [(1, 'EFTA00000002', 0, 'Ann Smith met Bob Jones'), (2, 'EFTA00000001', 0, 'cover'),
                              (3, 'EFTA00000001', 1, 'Ann Smith alone')])
            self.assertEqual(db.execute('SELECT id, source, type FROM documents ORDER BY id').fetchall(),
                             [('EFTA00000001', 'efta', None), ('EFTA00000002', 'efta', None)])

    def test_search_is_scoped_to_page_text(self):
        with closing(corpus.connect(self.path)) as db:
            self.assertEqual(corpus.search(db, '"Ann Smith"').tolist(), [1, 3])
            self.assertEqual(corpus.search(db, 'EFTA00000002').tolist(), [])  # the id column is not page text
            self.assertEqual(corpus.search_pages(db, '"Ann Smith" AND "Bob Jones"', 10), [('EFTA00000002', 0)])
            self.assertEqual(list(corpus.search_text(db, 'alone')), [('EFTA00000001', 'Ann Smith alone')])
        self.assertEqual([p.tolist() for p in corpus.parallel_search(['cover', 'nothing'], path=self.path)], [[2], []])

    def test_page_map_orders_documents_by_id(self):
        page_doc, names = page_map(self.path, Path(self.dir.name) / 'map.npz')
        self.assertEqual(names, ['EFTA00000001', 'EFTA00000002'])
        self.assertEqual(page_doc.tolist(), [-1, 1, 0, 0])

    def test_cited_pages_resolve_to_documents(self):
        with closing(corpus.connect(self.path)) as db:
            locate = corpus.page_locator(db)
            self.assertEqual(locate('EFTA00000002'), ('EFTA00000002', 0))
            self.assertEqual(locate('EFTA00000003'), ('EFTA00000003', None))
        self.assertEqual(corpus.cited_documents({'notes': 'see EFTA00000001', 'x': ['EFTA00000002']}),
                         ['EFTA00000001', 'EFTA00000002'])


if __name__ == '__main__':
    unittest.main()
