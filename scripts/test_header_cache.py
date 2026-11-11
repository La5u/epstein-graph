import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from header_cache import HeaderCache


class HeaderCacheTests(unittest.TestCase):
    def test_hits_including_false_and_unknown_contain_no_private_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checks.db'
            cache = HeaderCache(path, b'key', ['corpus', 'code'])
            for name, result in [('private true', True), ('private false', False), ('private unknown', None)]:
                compute = Mock(return_value=result)
                self.assertEqual(cache.get('private document', name, compute), result)
                self.assertEqual(cache.get('private document', name, compute), result)
                compute.assert_called_once()
            cache.close()
            self.assertNotIn(b'private', path.read_bytes())
            cache = HeaderCache(path, b'key', ['corpus', 'code'])
            compute = Mock(side_effect=AssertionError('cache miss'))
            for name, result in [('private true', True), ('private false', False), ('private unknown', None)]:
                self.assertEqual(cache.get('private document', name, compute), result)
            cache.close()

    def test_corpus_code_key_and_query_changes_invalidate_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checks.db'
            for key, inputs in [(b'key', ['corpus1', 'code1']), (b'key', ['corpus2', 'code1']),
                                (b'key', ['corpus2', 'code2']), (b'new key', ['corpus2', 'code2'])]:
                cache = HeaderCache(path, key, inputs)
                compute = Mock(return_value=True)
                self.assertTrue(cache.get('D1', 'Name', compute))
                compute.assert_called_once()
                self.assertFalse(cache.get('D2', 'Name', lambda: False))
                self.assertFalse(cache.get('D1', 'Other', lambda: False))
                cache.close()
