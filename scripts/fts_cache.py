"""Cache for full-text corpus queries: the corpus is read-only, so a query's rows
only change when the corpus file does. Rebuilds then only search new names.

    rows = cached(corpus, sql, params)

Stored in output/.build/fts_cache.db, keyed by the SQL and its parameters; cleared
when the corpus file changes size or mtime.
"""
import json
import sqlite3
import threading
from pathlib import Path

from adapters import efta

ROOT = Path(__file__).resolve().parents[1]
CORPUS = efta.PATH
PATH = ROOT / 'output/.build/fts_cache.db'  # under .build/: not a traced input or output
_local = threading.local()


def _db():
    db = getattr(_local, 'db', None)
    if db is None:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(PATH, timeout=60)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS queries(key TEXT PRIMARY KEY, rows TEXT)')
        stat = CORPUS.stat() if CORPUS.exists() else None
        stamp = json.dumps([stat.st_size, stat.st_mtime_ns] if stat else None)
        with db:
            if (db.execute("SELECT value FROM meta WHERE key='corpus'").fetchone() or [None])[0] != stamp:
                db.execute('DELETE FROM queries')
                db.execute("INSERT OR REPLACE INTO meta VALUES ('corpus', ?)", (stamp,))
        _local.db = db
    return db


def cached(corpus, sql, params=()):
    """corpus.execute(sql, params).fetchall(), from the cache when seen before.
    Only queries on the real corpus file are cached (never a test fixture)."""
    files = {row[2] for row in corpus.execute('PRAGMA database_list')}
    if str(CORPUS) not in files:
        try:
            return corpus.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
    key = json.dumps([sql, list(params)])
    db = _db()
    hit = db.execute('SELECT rows FROM queries WHERE key=?', (key,)).fetchone()
    if hit:
        return [tuple(row) for row in json.loads(hit[0])]
    try:
        rows = corpus.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        rows = []
    try:
        with db:
            db.execute('INSERT OR REPLACE INTO queries VALUES (?, ?)', (key, json.dumps(rows)))
    except sqlite3.OperationalError:
        pass  # busy with a parallel step: the answer is still right, just not stored
    return rows
