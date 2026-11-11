"""Private-keyed, corpus-versioned boolean memoization for header text checks.

Only opaque HMAC keys and true/false/unknown results are stored in .build;
neither header names nor document text is persisted. A config-only rebuild
can reuse corpus text checks independently of its current identity rules.
New results persist at close; an interrupted header pass may recompute them.
"""
import hashlib
import hmac
import json
import sqlite3
import sys
from pathlib import Path


MISSING = object()


class HeaderCache:
    def __init__(self, path, key, inputs):
        self.key, self.pending = key, []
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=60)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta(stamp TEXT)')
        schema = 'CREATE TABLE checks(key TEXT PRIMARY KEY, result INTEGER) WITHOUT ROWID'
        self.db.execute(schema.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS'))
        stamp = self.digest([inputs, sys.version, hashlib.blake2b(Path(__file__).read_bytes()).hexdigest()])
        with self.db:
            if self.db.execute('SELECT stamp FROM meta').fetchone() != (stamp,):
                self.db.execute('DROP TABLE checks')
                self.db.execute(schema)
                self.db.execute('DELETE FROM meta')
                self.db.execute('INSERT INTO meta VALUES (?)', (stamp,))
        # A few million opaque keys fit comfortably in memory during the header
        # pass. Avoid millions of random B-tree reads and dirty-page checkpoints;
        # persist new results together in primary-key order when the pass ends.
        self.results = dict(self.db.execute('SELECT key,result FROM checks'))

    def digest(self, values):
        return hmac.new(self.key, json.dumps(values, ensure_ascii=False).encode(), hashlib.sha256).hexdigest()

    def get(self, document, name, compute):
        key = self.digest([document, name])
        hit = self.results.get(key, MISSING)
        if hit is not MISSING:
            return hit
        result = compute()
        self.results[key] = result
        self.pending.append(key)
        return result

    def close(self):
        self.db.executemany('INSERT OR REPLACE INTO checks VALUES (?,?)',
                            ((key, self.results[key]) for key in sorted(self.pending)))
        self.db.commit()
        self.db.close()
        self.pending.clear()
        self.results.clear()
