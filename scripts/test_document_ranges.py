import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import corpus
import document_ranges as ranges
from adapters import efta


class DocumentRangeTests(unittest.TestCase):
    def test_cached_ranges_match_adapter_and_invalidate_on_source_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'corpus.db'
            cache = Path(directory) / 'ranges.npz'
            with closing(sqlite3.connect(path)) as db:
                efta.create(db, [], [('EFTA00000002', 3), ('EFTA00000010', 0),
                                     ('EFTA00000011', 2), ('EFTA00000013X', 99)])
            with patch.object(ranges, 'CORPUS', path), patch.object(ranges, 'CACHE', cache):
                for state in ('cold', 'warm', 'corrupt', 'changed'):
                    if state == 'corrupt':
                        cache.write_bytes(b'broken cache')
                    if state == 'changed':
                        with sqlite3.connect(path) as db:
                            db.execute("UPDATE documents SET total_pages=7 WHERE efta_number='EFTA00000002'")
                    with closing(corpus.connect(path)) as db:
                        actual, expected = ranges.page_locator(db), corpus.page_locator(db)
                        for value in [f'EFTA{i:08d}' for i in range(16)] + ['invalid', 'EFTA00000013X']:
                            self.assertEqual(actual(value), expected(value), (state, value))
                    self.assertTrue(cache.exists())

    def test_other_corpora_and_missing_connection_use_adapter(self):
        self.assertEqual(ranges.page_locator(None)('EFTA00000001'), ('EFTA00000001', None))
        with closing(sqlite3.connect(':memory:')) as db, patch.object(corpus, 'page_locator', return_value='fallback') as fallback:
            self.assertEqual(ranges.page_locator(db), 'fallback')
            fallback.assert_called_once_with(db)
