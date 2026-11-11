import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from functools import partial
from unittest.mock import Mock

from document_prefetch import DocumentPrefetch
import header_text


def failed_corpus():
    raise RuntimeError('unavailable corpus')


def empty_corpus():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE pages(doc_id,page,text)')
    return db


class DocumentPrefetchTests(unittest.TestCase):
    def test_worker_prepares_only_the_text_checks_needed_by_candidate_names(self):
        texts = {'A': 'To:\nblue parcel arrived tahnks\nOn Mon, Dec 22, 2014',
                 'B': 'From: Sender Person\nSubject: Budget review next week\nTo: Recipient Person'}
        names = {'A': ['blue parcel arrived tahnks'], 'B': ['Budget review next week']}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pages.db'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE pages(doc_id,page,text)')
                db.executemany('INSERT INTO pages VALUES (?,1,?)', texts.items())
            with DocumentPrefetch(partial(sqlite3.connect, path), texts.get, True) as prefetch:
                found = {document: prefetch.read(document) for _, document in
                         prefetch.rows(list(enumerate(texts)), lambda row: names[row[1]])}
            self.assertEqual(found['A'], (texts['A'], {
                'body_after_bare_field': header_text.body_after_bare_field(texts['A'])}))
            self.assertEqual(found['B'], (texts['B'], {
                'body_after_bare_field': header_text.body_after_bare_field(texts['B']),
                'nonparty_header_text': header_text.nonparty_header_text(texts['B'])}))

    def test_skipped_predictions_repeated_documents_and_fallback_keep_row_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pages.db'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE pages(doc_id,page,text)')
                db.executemany('INSERT INTO pages VALUES (?,1,?)', [('A', 'alpha'), ('B', 'beta'),
                                                                      ('C', 'gamma'), ('Z', 'last')])
            def read(document):
                with closing(sqlite3.connect(path)) as db:
                    return '\n'.join(t for t, in db.execute('SELECT text FROM pages WHERE doc_id=? ORDER BY page', (document,)))
            rows = list(enumerate(('A', 'B', 'A', 'C', 'Z')))
            found = []
            with DocumentPrefetch(partial(sqlite3.connect, path), read, True,
                                  batch_size=5, buffer_size=1) as prefetch:
                for index, document in prefetch.rows(rows, lambda row: row[1] != 'Z'):
                    if index:
                        found.append((document, prefetch.text(document)))
            self.assertEqual(found, [('B', 'beta'), ('A', 'alpha'), ('C', 'gamma'), ('Z', 'last')])

    def test_disabled_prefetch_does_not_connect_or_predict(self):
        connect = Mock(side_effect=AssertionError('unnecessary corpus connection'))
        needed = Mock(side_effect=AssertionError('unnecessary prediction'))
        with DocumentPrefetch(connect, lambda doc: doc, False) as prefetch:
            self.assertEqual(list(prefetch.rows([(0, 'A')], needed)), [(0, 'A')])
            self.assertEqual(prefetch.text('A'), 'A')

    def test_worker_errors_propagate_and_early_exit_cancels_pending_reads(self):
        connect = failed_corpus
        with self.assertRaisesRegex(RuntimeError, 'unavailable corpus'):
            with DocumentPrefetch(connect, lambda doc: '', True, buffer_size=1) as prefetch:
                for _, document in prefetch.rows([(0, 'A')], lambda row: True):
                    prefetch.text(document)
        # Exiting before reading any queued documents must also shut down cleanly.
        with DocumentPrefetch(empty_corpus, lambda doc: '', True, buffer_size=1) as prefetch:
            for row in prefetch.rows([(i, str(i)) for i in range(100)], lambda row: True):
                break


if __name__ == '__main__':
    unittest.main()
