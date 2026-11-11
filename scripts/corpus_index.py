"""Shared, cached page -> document map over the corpus, and near-duplicate clusters.

Mapping 3M page IDs to documents takes ~13 s and was done by both
build_comentions.py and involvement.py. The map is cached in
output/corpus_page_map.npz, keyed on the corpus file's size and mtime, and
rebuilt automatically when the corpus changes.
"""
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import numpy as np

import corpus

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / 'output/corpus_page_map.npz'


def page_map(path=corpus.PATH, cache=CACHE):
    """(page_doc, doc_names): page_doc[page_id] is the document index (-1 for
    unused IDs); indices follow first appearance in (doc_id, page_id) order."""
    stamp = np.array(corpus.stamp(path), dtype=np.int64)
    if cache.exists():
        with np.load(cache, allow_pickle=False) as data:
            if np.array_equal(data['stamp'], stamp):
                return data['page_doc'], data['doc_names'].tolist()
    db = corpus.connect(path)
    max_page = db.execute('SELECT max(page_id) FROM pages').fetchone()[0]
    page_doc = np.full(max_page + 1, -1, dtype=np.int32)
    doc_index, doc_names = {}, []
    for page_id, doc_id in db.execute('SELECT page_id, doc_id FROM pages ORDER BY doc_id, page_id'):
        index = doc_index.get(doc_id)
        if index is None:
            index = doc_index[doc_id] = len(doc_names)
            doc_names.append(doc_id)
        page_doc[page_id] = index
    db.close()
    partial = cache.with_suffix('.partial.npz')
    np.savez(partial, stamp=stamp, page_doc=page_doc, doc_names=np.array(doc_names))
    partial.replace(cache)
    return page_doc, doc_names


CLUSTERS = ROOT / 'output/document_clusters.db'


def doc_clusters(doc_names, path=CLUSTERS):
    """cluster[i]: near-duplicate cluster of document i (dedupe_documents.py), as
    the index of the cluster's canonical document; each document is its own
    cluster when the dedupe output is missing."""
    cluster = np.arange(len(doc_names), dtype=np.int64)
    if not path.exists():
        return cluster
    index = {name: i for i, name in enumerate(doc_names)}
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        for efta, canonical in db.execute('SELECT efta_number, canonical FROM docs WHERE efta_number != canonical'):
            if efta in index and canonical in index:
                cluster[index[efta]] = index[canonical]
    return cluster


def cluster_pair_counts(doc_members, cluster_of, pairs_of, max_members):
    """Co-mention counts with near-duplicate copies counted once.

    doc_members: document -> entities named in it; cluster_of(document) -> cluster;
    pairs_of(members) -> the pairs to count. A pair counts once per cluster when
    one copy names both; that copy (fewest entities, then lowest key) gives the
    specificity weight 1/(n-1) (only when n <= max_members) and the sample.
    Returns (shared, weighted, samples) like the per-document loops did."""
    by_cluster = defaultdict(list)
    for d, members in doc_members.items():
        if len(members) >= 2:
            by_cluster[cluster_of(d)].append(d)
    shared, weighted, samples = Counter(), Counter(), defaultdict(list)
    for copies in by_cluster.values():
        if len(copies) == 1:
            best = {pair: (len(doc_members[copies[0]]), copies[0]) for pair in pairs_of(doc_members[copies[0]])}
        else:
            best = {}
            for d in sorted(copies):
                n = len(doc_members[d])
                for pair in pairs_of(doc_members[d]):
                    if pair not in best or n < best[pair][0]:
                        best[pair] = (n, d)
        for pair, (n, d) in best.items():
            shared[pair] += 1
            if n <= max_members:
                weighted[pair] += 1 / (n - 1)
                samples[pair].append((n, d))
    return shared, weighted, samples


def canonical_documents(path=CLUSTERS):
    """{efta: canonical efta} for every document that is a copy or a subset of another
    (dedupe_documents.py); empty when the dedupe output is missing."""
    if not path.exists():
        return {}
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        return dict(db.execute('SELECT efta_number, canonical FROM docs WHERE efta_number != canonical'))
