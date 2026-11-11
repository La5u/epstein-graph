"""Bounded document checks in one worker during a cold header-cache build.

Only document text and candidate names enter the worker. Graph identities and
review exemptions stay in the parent, which keeps the original record order.
"""
import multiprocessing
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from itertools import islice
from multiprocessing.util import Finalize

import header_text


_worker_connect = _worker_pages = None


def _initialize(connect):
    global _worker_connect, _worker_pages
    _worker_connect, _worker_pages = connect, None
    Finalize(None, _close_worker, exitpriority=10)


def _close_worker():
    global _worker_pages
    if _worker_pages is not None:
        _worker_pages.close()
        _worker_pages = None


def _read_document(document, names):
    global _worker_pages
    if _worker_pages is None:
        _worker_pages = _worker_connect()
        _worker_pages.execute('PRAGMA cache_size=-32768')
    text = '\n'.join(t or '' for t, in _worker_pages.execute(
        'SELECT text FROM pages WHERE doc_id=? ORDER BY page', (document,)))
    blocks = {}
    if text and names:
        blocks['body_after_bare_field'] = header_text.body_after_bare_field(text)
        remaining = [name for name in names if header_text.word_key(name) not in blocks['body_after_bare_field']]
        if remaining:
            blocks['nonparty_header_text'] = header_text.nonparty_header_text(text)
            if any(header_text.word_key(name) not in blocks['nonparty_header_text']
                   and header_text.plain_text(name) for name in remaining):
                blocks['header_block_text'] = header_text.header_block_text(text)
    return text, blocks


class DocumentPrefetch:
    def __init__(self, connect, fallback, enabled, *, batch_size=1024, buffer_size=8):
        self.connect, self.fallback, self.enabled = connect, fallback, enabled
        self.batch_size, self.buffer_size = batch_size, buffer_size
        self.pool = None
        self.ranks, self.pending = {}, deque()
        self.last = -1

    def __enter__(self):
        if self.enabled:
            self.pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn'),
                                            initializer=_initialize, initargs=(self.connect,))
        return self

    def __exit__(self, *exc):
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)

    def _fill(self):
        while len(self.pending) < self.buffer_size and self.next < len(self.documents):
            document, names = self.documents[self.next]
            future = self.pool.submit(_read_document, document, names)
            self.pending.append((self.next, future))
            self.next += 1

    def rows(self, rows, needed):
        if not self.enabled:
            yield from rows
            return
        rows = iter(rows)
        while batch := list(islice(rows, self.batch_size)):
            documents = {}
            for row in batch:
                names = needed(row) if row[1] else ()
                if names:
                    # Boolean predictions are useful for callers needing only
                    # text. Name predictions also let the worker prepare checks.
                    documents.setdefault(row[1], set()).update(names if names is not True else ())
            self.documents = list(documents.items())
            self.ranks = {document: index for index, (document, _) in enumerate(self.documents)}
            self.last, self.next = -1, 0
            self._fill()
            yield from batch
            # A prediction can become unnecessary after wrapped names are joined.
            # Only the bounded window was submitted; discard the rest of the batch.
            while self.pending:
                _, future = self.pending.popleft()
                if not future.cancel():
                    future.result()
            self.ranks.clear()

    def read(self, document):
        target = self.ranks.get(document, -1)
        if target <= self.last:
            # The caller's LRU handles most repeats. If an earlier document was
            # skipped or evicted, its original connection remains a safe fallback.
            return self.fallback(document), {}
        while self.last < target:
            self.last, future = self.pending.popleft()
            result = future.result()
            self._fill()
        return result

    def text(self, document):
        return self.read(document)[0]
