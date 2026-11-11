"""Near-duplicate documents in the full-text corpus (MinHash + LSH).

The same email or filing appears in many productions under different EFTA
numbers, so "documents naming both" counts copies, not distinct documents.
This step groups copies into clusters:

1. Each page is normalized: Bates/production stamps removed (EFTA00001623,
   EFTA_R1_00912174, SDNY_GM_..., DB-SDNY-..., JPM-SDNY-..., DOJ-OGR-...,
   HOUSE_OVERSIGHT_...), lower-cased, reduced to [a-z0-9] words.
2. Boilerplate is ignored: shingles on more than COMMON_FRACTION of the pages
   in a sample (every SAMPLE_EVERY-th chunk of page ids) are dropped, the
   IDF idea (email disclaimers, confidentiality legends, "jeevacation gmail
   com"). Without this, short emails that share a disclaimer look alike.
3. A document's remaining word 3-shingles are MinHashed (K hashes; the signature of a
   document is the element-wise minimum over its pages, i.e. the MinHash of the
   union of its pages' shingles). Documents with fewer than MIN_SHINGLES
   shingles only match by exact normalized text, and documents with fewer than
   MIN_CONTENT (blank pages, "No Images Produced", stamp-only pages) match nothing.
4. LSH (BANDS bands of ROWS rows) proposes candidates; a pair joins when the
   estimated Jaccard similarity is >= --threshold and the shingle counts are
   within LENGTH_RATIO of each other (so a short email is not swallowed by a
   long thread that quotes it: containment is not duplication), and the two
   documents do not both carry dates without sharing one (DATES smallest date
   hashes per document): monthly statements of one account share their template
   but not their dates; replies in one thread quote the earlier dates.
5. Clusters: documents with identical normalized text, plus, inside each group of
   LSH-linked documents, greedy center clustering (largest document first; each
   document joins the most similar center that passes, else starts a center), so
   a chain A~B~C does not join A and C.
6. Supersets: a cluster whose text is (almost) all inside a larger document (an
   email quoted in a later reply, a page inside a compiled exhibit) joins the
   cluster of that larger document, which keeps everything it says. Each
   document keeps SAMPLES uniformly sampled shingles (the arg-min shingles of its
   first SAMPLES MinHash functions); a second corpus pass finds every document
   containing a sampled shingle; a cluster is contained in a larger one when >=
   CONTAIN of its canonical's samples occur there and its earliest date (if
   any) is not missing from the larger one. Each cluster joins at most one
   container, the largest one that is not itself inside another (so no chains,
   and two threads quoting the same email are not merged with each other).
   The canonical document is the one with the most shingles (the superset;
   among copies the fullest OCR), ties to the lowest EFTA number.

Writes output/document_clusters.db (table docs: efta_number, cluster, canonical,
shingles, similarity to canonical (estimated Jaccard), exact (1 when the
normalized text equals the canonical's), relation (canonical, exact, near,
contained: inside the canonical or inside a document that is)) and
output/document_clusters_summary.json. Signatures and sample postings are cached
in output/.build/doc_minhash.npz, keyed on the corpus stamp.

    .venv/bin/python scripts/dedupe_documents.py [--workers 8] [--threshold 0.8] [--limit-pages N]
"""
import argparse
import hashlib
import json
import os
import re
import resource
import sqlite3
import time
import zlib
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from pathlib import Path

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

import corpus as corpus_source
from corpus_index import page_map

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'
CACHE = OUT / '.build/doc_minhash.npz'
CLUSTERS = OUT / 'document_clusters.db'
SUMMARY = OUT / 'document_clusters_summary.json'

K, BANDS, ROWS = 64, 16, 4  # LSH threshold ~ (1/16)^(1/4) = 0.5, well below the verify threshold
SHINGLE = 3
MIN_SHINGLES = 8
MIN_CONTENT = 3  # below this many non-boilerplate shingles a document has no content (blank, stamp-only) and is never clustered
LENGTH_RATIO = 0.7
CHUNKS, SAMPLE_EVERY = 256, 8
MAX_ANCHORS = 64  # per LSH bucket (bounds the cost of large buckets of unrelated documents)
COMMON_FRACTION = 0.0005  # boilerplate: shingle on >= 0.05% of sampled pages (~1,500 corpus pages)
DATES = 8
SAMPLES = 16  # sampled shingles per document for the superset test
CONTAIN = 0.8  # share of a document's samples found in a larger one for it to count as contained
MIN_CONTAINED = 20  # a document needs this many content shingles to be tested as contained
MAX_POSTINGS = 500  # sampled shingles found in more clusters than this are not informative (quoted everywhere)
EXPAND_ROWS = 20_000_000  # rows expanded per matching chunk (bounds memory)
CACHE_VERSION = 4
SAMPLED_PATH = OUT / '.build/doc_samples.npy'

# Production stamps (OCR-tolerant: "E FTA", "SONY GM", spaces inside the number).
STAMP = re.compile(
    r"(?i)(?<![a-z0-9])(?:e\s?fta|sdny|sony|db|jpm|doj|house|usao|td|ubs[a-z]*)"
    r"(?:[ _\-]+(?:r\d+|gm|sdny|sony|ogr|oversight|doj|efta|nhd))*[ _\-]*\d[\d ]{3,10}\d(?![0-9])")
NON_WORD = re.compile(r'[^a-z0-9]+')
MONTHS = {m: i + 1 for i, m in enumerate(('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'))}
MONTH = r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?'
DATE = re.compile(rf'(?i)(?<![0-9])(\d{{1,2}})[/.-](\d{{1,2}})[/.-](\d{{4}}|\d{{2}})(?![0-9])'
                  rf'|\b{MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b'
                  rf'|\b(\d{{1,2}})\s+{MONTH},?\s+(\d{{4}})\b')
M1, M2 = np.uint64(0x9E3779B97F4A7C15), np.uint64(0xBF58476D1CE4E5B9)
_rng = np.random.default_rng(20261023)
A = _rng.integers(1, 2**63, K, dtype=np.uint64) | np.uint64(1)
B = _rng.integers(0, 2**63, K, dtype=np.uint64)
EMPTY = np.uint32(0xFFFFFFFF)


def normalize(text):
    return NON_WORD.sub(' ', STAMP.sub(' ', text or '').lower()).strip()


def date_hashes(text):
    """The DATES smallest hashes (uint32, never 0) of the distinct dates in text, as (month, day, 2-digit year)."""
    found = set()
    for m in DATE.finditer(text or ''):
        g = m.groups()
        if g[0]:
            month, day, year = int(g[0]), int(g[1]), g[2]
        elif g[3]:
            month, day, year = MONTHS[g[3][:3].lower()], int(g[4]), g[5]
        else:
            month, day, year = MONTHS[g[7][:3].lower()], int(g[6]), g[8]
        if 1 <= month <= 12 and 1 <= day <= 31:
            found.add(zlib.crc32(f'{month}/{day}/{year[-2:]}'.encode()) | 1)
    return sorted(found)[:DATES]


def shingle_hashes(words):
    """uint64 hash per word 3-shingle (words: list of bytes)."""
    if len(words) < SHINGLE:
        return np.empty(0, dtype=np.uint64)
    t = np.fromiter((zlib.crc32(w) for w in words), dtype=np.uint64, count=len(words))
    with np.errstate(over='ignore'):
        h = t[:-2] * M1 ^ t[1:-1]
        h = h * M2 ^ t[2:]
        h ^= h >> np.uint64(31)
        h *= M1
    return np.unique(h)


def minhash(hashes):
    """K-value MinHash (uint32) of a set of uint64 hashes, and the shingles (hashes) that
    attain the first SAMPLES minima (0 when empty); processed in blocks to bound memory."""
    sig = np.full(K, EMPTY, dtype=np.uint32)
    sample = np.zeros(SAMPLES, dtype=np.uint64)
    with np.errstate(over='ignore'):
        for start in range(0, len(hashes), 4096):
            block = hashes[start:start + 4096]
            values = ((block[:, None] * A + B) >> np.uint64(32)).astype(np.uint32)
            at = values.argmin(axis=0)
            best = values[at, np.arange(K)]
            better = best < sig
            sig[better] = best[better]
            sample[better[:SAMPLES]] = block[at[:SAMPLES][better[:SAMPLES]]]
    return sig, sample


COMMON = np.empty(0, dtype=np.uint64)
SAMPLED = np.empty(0, dtype=np.uint64)


def set_common(common, sampled_path=None):
    global COMMON, SAMPLED
    COMMON = common
    if sampled_path:
        SAMPLED = np.load(sampled_path, mmap_mode='r')  # shared through the page cache, not pickled per worker


def in_sorted(values, table):
    """Boolean mask: which values occur in the sorted array table."""
    if not len(table) or not len(values):
        return np.zeros(len(values), dtype=bool)
    at = np.minimum(np.searchsorted(table, values), len(table) - 1)
    return np.asarray(table[at]) == values


def drop_common(hashes, common):
    if not len(common) or not len(hashes):
        return hashes
    at = np.minimum(np.searchsorted(common, hashes), len(common) - 1)
    return hashes[common[at] != hashes]


def sample_hashes(bounds):
    """Unique shingle hashes of every page in [low, high), concatenated (page-level document frequency)."""
    low, high = bounds
    with closing(corpus_source.connect()) as corpus:
        parts = [shingle_hashes(normalize(text).encode().split()) for (text,) in
                 corpus.execute('SELECT text FROM pages WHERE page_id >= ? AND page_id < ?', (low, high))]
    return (np.concatenate(parts) if parts else np.empty(0, dtype=np.uint64)), len(parts)


def common_shingles(pool, bounds):
    parts = list(pool.map(sample_hashes, bounds[::SAMPLE_EVERY]))
    pages = sum(n for _, n in parts)
    values, counts = np.unique(np.concatenate([h for h, _ in parts]), return_counts=True)
    return values[counts >= max(2, COMMON_FRACTION * pages)], pages


def scan(bounds):
    """Per page in [low, high): doc index, content-shingle count, exact digest, signature."""
    low, high = bounds
    page_doc, _ = page_map()
    docs, counts, digests, sigs, samples, date_docs, dates = [], [], [], [], [], [], []
    with closing(corpus_source.connect()) as corpus:
        for page_id, page_number, text in corpus.execute(
                'SELECT page_id, page, text FROM pages WHERE page_id >= ? AND page_id < ?', (low, high)):
            doc = int(page_doc[page_id]) if page_id < len(page_doc) else -1
            if doc < 0:
                continue
            norm = normalize(text)
            hashes = drop_common(shingle_hashes(norm.encode().split()), COMMON)
            docs.append(doc)
            counts.append(len(hashes))
            # Per document the page digests are summed; each binds its page number to the page text.
            digests.append(int.from_bytes(hashlib.blake2b(f'{page_number}\0{norm}'.encode(), digest_size=8).digest()) if norm else 0)
            sig, sample = minhash(hashes)
            sigs.append(sig)
            samples.append(sample)
            for value in date_hashes(text):
                date_docs.append(doc)
                dates.append(value)
    return (np.array(docs, dtype=np.int32), np.array(counts, dtype=np.int64), np.array(digests, dtype=np.uint64),
            np.array(sigs, dtype=np.uint32).reshape(-1, K), np.array(samples, dtype=np.uint64).reshape(-1, SAMPLES),
            np.array(date_docs, dtype=np.int64), np.array(dates, dtype=np.uint32))


def postings_scan(bounds):
    """(document, shingle) for every sampled shingle (SAMPLED) on the pages in [low, high)."""
    low, high = bounds
    page_doc, _ = page_map()
    docs, found = [], []
    with closing(corpus_source.connect()) as corpus:
        for page_id, text in corpus.execute('SELECT page_id, text FROM pages WHERE page_id >= ? AND page_id < ?', (low, high)):
            doc = int(page_doc[page_id]) if page_id < len(page_doc) else -1
            if doc < 0:
                continue
            hashes = drop_common(shingle_hashes(normalize(text).encode().split()), COMMON)
            hit = hashes[in_sorted(hashes, SAMPLED)]
            docs.append(np.full(len(hit), doc, dtype=np.int32))
            found.append(hit)
    if not docs:
        return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.uint64)
    return np.concatenate(docs), np.concatenate(found)


def bottom_dates(n, date_docs, dates):
    """(n, DATES) uint32: per document its DATES smallest distinct date hashes, 0-padded."""
    out = np.zeros((n, DATES), dtype=np.uint32)
    if not len(dates):
        return out
    pairs = np.unique(date_docs * 2**32 + dates.astype(np.int64))
    doc, value = pairs >> 32, (pairs & 0xFFFFFFFF).astype(np.uint32)
    starts = np.flatnonzero(np.r_[True, doc[1:] != doc[:-1]])
    rank = np.arange(len(doc)) - np.repeat(starts, np.diff(np.r_[starts, len(doc)]))
    keep = rank < DATES
    out[doc[keep], rank[keep]] = value[keep]
    return out


def signatures(args):
    stamp = np.array([*corpus_source.stamp(), args.limit_pages, CACHE_VERSION], dtype=np.int64)
    if CACHE.exists() and not args.rescan:
        with np.load(CACHE, allow_pickle=False) as data:
            if np.array_equal(data['stamp'], stamp):
                return Features(data['sig'], data['shingles'], data['digest'], data['dates'], data['sample'],
                                data['post_docs'], data['post_hashes'])
    page_doc, doc_names = page_map()
    with closing(corpus_source.connect()) as corpus:
        max_page = corpus.execute('SELECT max(page_id) FROM pages').fetchone()[0]
    if args.limit_pages:
        max_page = min(max_page, args.limit_pages)
    n = len(doc_names)
    sig = np.full((n, K), EMPTY, dtype=np.uint32)
    shingles = np.zeros(n, dtype=np.int64)  # sum over pages (shingles shared by pages counted twice; size proxy)
    digest = np.zeros(n, dtype=np.uint64)
    sample = np.zeros((n, SAMPLES), dtype=np.uint64)
    date_docs, dates = [], []
    chunks = CHUNKS
    bounds = [(1 + i * max_page // chunks, 1 + (i + 1) * max_page // chunks) for i in range(chunks)]
    start, done = time.time(), 0
    with ProcessPoolExecutor(args.workers) as pool:
        common, sampled = common_shingles(pool, bounds)
    print(f'{len(common)} boilerplate shingles from {sampled} sampled pages, {time.time() - start:.0f}s', flush=True)
    with ProcessPoolExecutor(args.workers, initializer=set_common, initargs=(common,)) as pool:
        for docs, counts, digests, sigs, samples, page_date_docs, page_dates in pool.map(scan, bounds):
            date_docs.append(page_date_docs)
            dates.append(page_dates)
            np.minimum.at(sig, docs, sigs)
            # A page's sampled shingle is its document's when the page attains the document's minimum.
            rows, k = np.nonzero((sigs[:, :SAMPLES] == sig[docs, :SAMPLES]) & (sigs[:, :SAMPLES] != EMPTY))
            sample[docs[rows], k] = samples[rows, k]
            np.add.at(shingles, docs, counts)
            with np.errstate(over='ignore'):
                np.add.at(digest, docs, digests)
            done += 1
            if done % 32 == 0:
                print(f'  {done}/{chunks} chunks, {time.time() - start:.0f}s', flush=True)
    dates = bottom_dates(n, np.concatenate(date_docs), np.concatenate(dates))

    # Second pass: which documents contain each sampled shingle of a testable document.
    sampled = np.unique(sample[shingles >= MIN_CONTAINED])
    sampled = sampled[sampled != 0]
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.save(SAMPLED_PATH, sampled)
    post_docs, post_hashes, done = [], [], 0
    with ProcessPoolExecutor(args.workers, initializer=set_common, initargs=(common, str(SAMPLED_PATH))) as pool:
        for docs, hashes in pool.map(postings_scan, bounds):
            post_docs.append(docs)
            post_hashes.append(hashes)
            done += 1
            if done % 64 == 0:
                print(f'  postings {done}/{chunks} chunks, {time.time() - start:.0f}s', flush=True)
    SAMPLED_PATH.unlink(missing_ok=True)
    post_docs, post_hashes = np.concatenate(post_docs), np.concatenate(post_hashes)
    print(f'{len(sampled)} sampled shingles, {len(post_docs)} postings, {time.time() - start:.0f}s', flush=True)
    partial = CACHE.with_suffix('.partial.npz')
    np.savez(partial, stamp=stamp, sig=sig, shingles=shingles, digest=digest, dates=dates, common=common, sample=sample,
             post_docs=post_docs, post_hashes=post_hashes)
    partial.replace(CACHE)
    return Features(sig, shingles, digest, dates, sample, post_docs, post_hashes)


class Features:
    """Per document: MinHash signature, content-shingle count, exact digest, date sample,
    sampled shingles; and postings (document, sampled shingle it contains)."""

    def __init__(self, sig, shingles, digest, dates, sample=None, post_docs=None, post_hashes=None):
        self.sig, self.shingles, self.digest, self.dates = sig, shingles, digest, dates
        self.sample, self.post_docs, self.post_hashes = sample, post_docs, post_hashes


def similar(f, a, b, threshold):
    """Boolean mask over pairs (a[i], b[i])."""
    jaccard = (f.sig[a] == f.sig[b]).mean(axis=1)
    small, large = np.minimum(f.shingles[a], f.shingles[b]), np.maximum(f.shingles[a], f.shingles[b])
    da, db = f.dates[a], f.dates[b]
    shared_date = ((da[:, :, None] == db[:, None, :]) & (da[:, :, None] != 0)).any(axis=(1, 2))
    dated = (da[:, 0] != 0) & (db[:, 0] != 0)
    return (jaccard >= threshold) & (small >= LENGTH_RATIO * large) & (shared_date | ~dated)


def candidate_pairs(f, threshold):
    """Pairs that pass `similar`, found through LSH buckets. Within a bucket the first
    member is the anchor and every other member is compared with it; members that
    match no anchor yet supply the next anchor (not all pairs)."""
    sig, eligible = f.sig, np.flatnonzero(f.shingles >= MIN_SHINGLES)
    found_a, found_b = [], []
    for band in range(BANDS):
        rows = sig[eligible, band * ROWS:(band + 1) * ROWS]
        key = np.ascontiguousarray(rows).view(np.dtype((np.void, rows.dtype.itemsize * ROWS))).ravel()
        order = np.argsort(key, kind='stable')
        sorted_key = key[order]
        starts = np.flatnonzero(np.r_[True, sorted_key[1:] != sorted_key[:-1]])
        sizes = np.diff(np.r_[starts, len(order)])
        for s, size in zip(starts[sizes > 1], sizes[sizes > 1]):
            rest, anchors = eligible[order[s:s + size]], 0
            while len(rest) > 1 and anchors < MAX_ANCHORS:
                anchors += 1
                anchor, members = rest[0], rest[1:]
                ok = similar(f, np.full(len(members), anchor), members, threshold)
                found_a.append(np.full(ok.sum(), anchor))
                found_b.append(members[ok])
                rest = members[~ok]
    if not found_a:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    return np.concatenate(found_a), np.concatenate(found_b)


def center_pairs(f, a, b, n, threshold):
    """(member, center) links: greedy center clustering inside each connected group of near pairs."""
    if not len(a):
        return a, b
    graph = coo_matrix((np.ones(len(a), dtype=np.int8), (a, b)), shape=(n, n))
    _, groups = connected_components(graph, directed=False)
    linked = np.unique(np.r_[a, b])
    order = linked[np.lexsort((linked, -f.shingles[linked], groups[linked]))]
    bounds = np.flatnonzero(np.r_[True, groups[order][1:] != groups[order][:-1], True])
    members, centers = [], []
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        group = order[lo:hi]
        chosen = [group[0]]
        for doc in group[1:]:
            pool = np.array(chosen)
            ok = similar(f, pool, np.full(len(pool), doc), threshold)
            if ok.any():
                score = (f.sig[pool[ok]] == f.sig[doc]).mean(axis=1)
                members.append(doc)
                centers.append(pool[ok][np.argmax(score)])
            else:
                chosen.append(doc)
    return np.array(members, dtype=np.int64), np.array(centers, dtype=np.int64)


def date_inside(small, large):
    """Mask: the earliest-hashed date of each `small` row may occur in the `large` row (rows of
    DATES smallest hashes): true when small has no date, or its first date is among large's
    dates, or large's sample is full and ends below it (cannot tell)."""
    first = small[:, :1]
    found = (large == first).any(axis=1)
    full = large[:, -1] != 0
    return (first[:, 0] == 0) | found | (full & (first[:, 0] > large[:, -1]))


def containment_links(f, labels, canonical_of_label):
    """(contained canonical, container canonical): each cluster whose canonical has >= CONTAIN of
    its samples inside a larger cluster joins the best such cluster. Memory stays bounded: postings
    are (shingle id, cluster) int64 keys, and canonicals are matched in chunks of at most
    EXPAND_ROWS expanded rows, keeping only the pairs that pass."""
    if f.post_docs is None or not len(f.post_docs):
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    n = len(labels)
    # Dense ids for the sampled shingles, then unique (shingle id, cluster) postings sorted by id.
    vocab = np.unique(f.post_hashes)
    keys = np.searchsorted(vocab, f.post_hashes).astype(np.int64)
    keys *= n
    keys += labels[f.post_docs]
    f.post_docs = f.post_hashes = None  # the largest arrays; not needed again
    keys = np.unique(keys)
    post_id, post_label = keys // n, keys % n
    del keys
    starts = np.searchsorted(post_id, np.arange(len(vocab) + 1))
    width = np.diff(starts)  # clusters containing each shingle
    # Testable canonicals and their sample rows (canonical, shingle id).
    reps = canonical_of_label[np.unique(labels)]
    reps = reps[f.shingles[reps] >= MIN_CONTAINED]
    found_a, found_b, found_share = [], [], []
    row_rep, k = np.nonzero(f.sample[reps] != 0)
    sample = f.sample[reps[row_rep], k]
    at = np.minimum(np.searchsorted(vocab, sample), len(vocab) - 1)
    present = vocab[at] == sample
    sid = np.where(present, at, -1)
    valid = np.bincount(row_rep, minlength=len(reps))  # samples per canonical
    cost = np.where(sid >= 0, width[np.maximum(sid, 0)], 0)
    cost[cost > MAX_POSTINGS] = 0
    sid[cost == 0] = -1
    # Chunks of whole canonicals (rows of one canonical are contiguous in row_rep).
    per_rep = np.bincount(row_rep, weights=cost, minlength=len(reps))
    bounds = np.searchsorted(np.cumsum(per_rep), np.arange(EXPAND_ROWS, per_rep.sum() + EXPAND_ROWS, EXPAND_ROWS), 'right')
    rep_lo, row_starts = 0, np.searchsorted(row_rep, np.arange(len(reps) + 1))
    for rep_hi in np.unique(np.r_[bounds, len(reps)]):
        rows = slice(row_starts[rep_lo], row_starts[min(rep_hi, len(reps))])
        rep_lo = rep_hi
        r, s_ = row_rep[rows], sid[rows]
        use = s_ >= 0
        r, s_ = r[use], s_[use]
        if not len(r):
            continue
        count = width[s_]
        a_label = np.repeat(labels[reps[r]], count)
        offsets = np.arange(count.sum()) - np.repeat(np.cumsum(count) - count, count)
        b_label = post_label[np.repeat(starts[s_], count) + offsets]
        rep_of_row = np.repeat(r, count)
        other = a_label != b_label
        pair = rep_of_row[other] * n + b_label[other]
        pair, matched = np.unique(pair, return_counts=True)
        rep_idx, b_label = pair // n, pair % n
        share = matched / np.maximum(valid[rep_idx], 1)
        a, b = reps[rep_idx], canonical_of_label[b_label]
        ok = (share >= CONTAIN) & (f.shingles[b] > f.shingles[a]) & date_inside(f.dates[a], f.dates[b])
        found_a.append(a[ok])
        found_b.append(b[ok])
        found_share.append(share[ok])
    if not found_a:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    a, b, share = np.concatenate(found_a), np.concatenate(found_b), np.concatenate(found_share)
    # Only containers that are not themselves inside something larger (no chains A in B in C
    # where A is not in C); one per contained cluster: the largest, then most samples found.
    top = ~in_sorted(b, np.unique(a))
    a, b, share = a[top], b[top], share[top]
    if not len(a):
        return a, b
    order = np.lexsort((b, -share, -f.shingles[b], a))
    first = np.r_[True, a[order][1:] != a[order][:-1]]
    return a[order][first], b[order][first]


def exact_pairs(digest, shingles):
    """Documents with identical normalized text and at least MIN_CONTENT content shingles."""
    nonempty = np.flatnonzero((digest != 0) & (shingles >= MIN_CONTENT))
    order = nonempty[np.argsort(digest[nonempty], kind='stable')]
    same = digest[order[1:]] == digest[order[:-1]]
    return order[:-1][same], order[1:][same]


def components(n, a, b, shingles):
    """(label per document, canonical document per label): connected components of the links;
    canonical = most shingles, then lowest index (doc_names are in sorted EFTA order)."""
    graph = coo_matrix((np.ones(len(a), dtype=np.int8), (a, b)), shape=(n, n))
    labels = connected_components(graph, directed=False)[1].astype(np.int64)  # int32 would overflow label * n
    order = np.lexsort((np.arange(n), -shingles, labels))
    first = np.r_[True, labels[order][1:] != labels[order][:-1]]
    canonical_of_label = np.empty(labels.max() + 1, dtype=np.int64)
    canonical_of_label[labels[order][first]] = order[first]
    return labels, canonical_of_label


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--threshold', type=float, default=0.8)
    parser.add_argument('--limit-pages', type=int, default=0, help='scan only the first N page IDs (testing)')
    parser.add_argument('--rescan', action='store_true', help='ignore the signature cache')
    args = parser.parse_args()
    # Fail with MemoryError instead of driving the machine into the OOM killer (peak ~5.5 GB on 1.4M documents).
    limit = int(float(os.environ.get('DEDUPE_MEMORY_GB', '7')) * 2**30)
    resource.setrlimit(resource.RLIMIT_AS, (limit, resource.getrlimit(resource.RLIMIT_AS)[1]))
    start = time.time()
    _, doc_names = page_map()
    f = signatures(args)
    sig, shingles, digest = f.sig, f.shingles, f.digest
    print(f'signatures for {len(doc_names)} documents in {time.time() - start:.0f}s', flush=True)

    n = len(doc_names)
    ea, eb = exact_pairs(digest, shingles)
    na, nb = candidate_pairs(f, args.threshold)
    ca, cb = center_pairs(f, na, nb, n, args.threshold)
    near_labels, near_canonical = components(n, np.r_[ea, ca], np.r_[eb, cb], shingles)
    print(f'{len(ea)} exact pairs, {len(na)} near pairs -> {len(ca)} center links; {time.time() - start:.0f}s', flush=True)
    sa, sb = containment_links(f, near_labels, near_canonical)
    labels, canonical_of_label = components(n, np.r_[ea, ca, sa], np.r_[eb, cb, sb], shingles)
    print(f'{len(sa)} clusters inside a larger document; {time.time() - start:.0f}s', flush=True)

    names = np.array(doc_names)
    canonical = canonical_of_label[labels]
    jaccard = (sig == sig[canonical]).mean(axis=1)
    exact = (digest == digest[canonical]) & (digest != 0)
    near = near_labels == near_labels[canonical]
    relation = np.where(np.arange(n) == canonical, 'canonical', np.where(exact, 'exact', np.where(near, 'near', 'contained')))

    sizes = np.bincount(labels)
    in_cluster = sizes[labels] > 1
    partial = CLUSTERS.with_suffix('.partial.db')
    partial.unlink(missing_ok=True)
    with closing(sqlite3.connect(partial)) as db:
        db.execute('CREATE TABLE docs(efta_number TEXT PRIMARY KEY, cluster INTEGER, canonical TEXT, shingles INTEGER, '
                   'similarity REAL, exact INTEGER, relation TEXT)')
        db.executemany('INSERT INTO docs VALUES (?,?,?,?,?,?,?)', (
            (names[i], int(canonical[i]), names[canonical[i]], int(shingles[i]), round(float(jaccard[i]), 3), int(exact[i]),
             str(relation[i])) for i in range(n)))
        db.execute('CREATE INDEX docs_cluster ON docs(cluster)')
        db.commit()
    partial.replace(CLUSTERS)

    distinct = len(np.unique(labels))
    hist = Counter(int(s) for s in sizes[sizes > 1])
    summary = {
        'documents': n, 'distinct_documents': distinct, 'documents_in_clusters': int(in_cluster.sum()),
        'clusters': int((sizes > 1).sum()), 'duplicates_removed': n - distinct,
        'documents_without_content': int((shingles < MIN_CONTENT).sum()), 'threshold': args.threshold, 'shingle': SHINGLE, 'k': K,
        'bands': BANDS, 'rows': ROWS, 'length_ratio': LENGTH_RATIO, 'dates': DATES,
        'boilerplate_fraction': COMMON_FRACTION, 'exact_pairs': len(ea), 'near_pairs': len(na),
        'samples': SAMPLES, 'contain': CONTAIN, 'clusters_inside_larger': len(sa),
        'documents_by_relation': dict(Counter(relation.tolist())),
        'largest_clusters': [{'canonical': str(names[canonical_of_label[l]]), 'size': int(sizes[l])}
                             for l in np.argsort(-sizes)[:20]],
        'cluster_size_histogram': {str(k): v for k, v in sorted(hist.items())[:30]},
        'seconds': round(time.time() - start),
    }
    SUMMARY.write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k not in ('largest_clusters', 'cluster_size_histogram')}))


if __name__ == '__main__':
    main()
