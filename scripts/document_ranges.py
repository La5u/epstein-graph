"""Cached EFTA document ranges for the document index's Bates-page resolver.

The adapter's uncached resolver remains the reference for other corpus files.
Only the real, immutable corpus uses this compact cache under output/.build.
"""
import bisect
import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import numpy as np

import corpus
from adapters import efta

CORPUS = corpus.PATH
CACHE = Path(__file__).resolve().parents[1] / 'output/.build/document_ranges.npz'


def ranges(db):
    stat = CORPUS.stat()
    stamp = json.dumps([stat.st_size, stat.st_mtime_ns,
                        *[hashlib.blake2b(Path(p).read_bytes()).hexdigest() for p in (__file__, efta.__file__)]])
    try:
        with np.load(CACHE, allow_pickle=False) as cached:
            if cached['stamp'].item() == stamp:
                return cached['starts'], cached['sizes']
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
        pass
    try:
        rows = sorted((int(e[4:]), n or 1) for e, n in db.execute(
            "SELECT efta_number,total_pages FROM main.documents WHERE efta_number GLOB 'EFTA[0-9]*'")
                      if efta.EFTA.fullmatch(e))
    except sqlite3.OperationalError:
        rows = []
    starts = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    sizes = np.fromiter((r[1] for r in rows), dtype=np.int64, count=len(rows))
    del rows
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE.with_suffix('.partial.npz')
    np.savez_compressed(temporary, stamp=stamp, starts=starts, sizes=sizes)
    temporary.replace(CACHE)
    return starts, sizes


def page_locator(db):
    if db is None or str(CORPUS.resolve()) not in {r[2] for r in db.execute('PRAGMA database_list')}:
        return corpus.page_locator(db)
    starts = sizes = None

    def locate(value):
        nonlocal starts, sizes
        if not efta.EFTA.fullmatch(value):
            return value, None
        if starts is None:
            starts, sizes = ranges(db)
        number = int(value[4:])
        i = bisect.bisect_right(starts, number) - 1
        if i >= 0 and number - starts[i] < sizes[i]:
            return f'EFTA{starts[i]:08d}', int(number - starts[i])
        return value, None
    return locate
