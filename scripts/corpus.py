"""The common document format pipeline steps read; sources plug in as adapters
(scripts/adapters/). Steps never open a source file or use its column names.

connect() gives a read-only connection with these views:
  documents(id, source, title, url, date, type, meta)   meta: JSON of source-specific fields
  pages(page_id, doc_id, page, text)                    page_id: stable integer handle for a page
Page search takes an FTS5 query over page text, without column filters (the
adapter scopes it), and returns page_ids or (doc_id, page) rows in page_id order.

One adapter so far (adapters/efta.py); `path` arguments let tests point it at a fixture.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from adapters import efta
from fts_cache import cached

PATH = efta.PATH
cited_documents = efta.cited_ids  # document ids cited anywhere in a record (str, JSON, lists, dicts)
page_locator = efta.page_locator  # db -> locate(id) -> (document, 0-based page or None)


def connect(path=PATH):
    db = efta.connect(path)
    efta.install_views(db)
    return db


def stamp(path=PATH):
    """[size, mtime_ns] of the source files: caches derived from the corpus key on this."""
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def search(db, query):
    """Matching page_ids as an int array; rows are joined inside SQLite (no
    per-row Python), which also releases the GIL so threads run in parallel."""
    text = db.execute('SELECT group_concat(rowid) FROM main.pages_fts f WHERE f.pages_fts MATCH ?',
                      (efta.scope(query),)).fetchone()[0]
    return np.array(text.split(','), dtype=np.int64) if text else np.empty(0, dtype=np.int64)


def parallel_search(queries, workers=4, path=PATH):
    """[search(query)] in input order, over one connection per thread."""
    local = threading.local()

    def run(query):
        if not hasattr(local, 'db'):
            local.db = connect(path)
        return search(local.db, query)
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(run, queries))


def search_pages(db, query, limit):
    """[(doc_id, page)] of the first `limit` matching pages (cached across runs)."""
    return cached(db, 'SELECT p.doc_id, p.page FROM main.pages_fts f JOIN pages p ON p.page_id=f.rowid '
                      'WHERE f.pages_fts MATCH ? LIMIT ?', (efta.scope(query), limit))


def search_text(db, query):
    """[(doc_id, text)] of every matching page."""
    return db.execute('SELECT p.doc_id, p.text FROM main.pages_fts f JOIN pages p ON p.page_id=f.rowid '
                      'WHERE f.pages_fts MATCH ?', (efta.scope(query),))


def document_urls(ids):
    """{doc_id: official url} for the requested documents."""
    return efta.document_urls(ids)
