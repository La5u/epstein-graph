"""Document co-mention layer from the full-text corpus (FTS5 phrase search).

Two graph entities are linked when their names appear as exact phrases in the
same EFTA document. This is a LEAD for reading documents, not evidence of a
relationship: names co-occur in court filings, contact lists, flight logs and
news clippings. OCR errors and common names cause false matches; spelling
variants not in the alias registry cause misses.

Victim-labelled people and unsafe/garbled labels are never searched.

Near-duplicate copies of one document (dedupe_documents.py) count once: a pair's
shared_documents, specificity and lift are over distinct documents; the
mentions table still lists every copy.
"""
import argparse
import csv
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from itertools import combinations
from pathlib import Path

import numpy as np

import corpus
from corpus_index import cluster_pair_counts, doc_clusters, page_map
from normalize_graph import name_key, registry_aliases
from survivor_policy import survivor_ids

ROOT = Path(__file__).resolve().parents[1]
SEARCHED_TYPES = ('person', 'organization', 'shell_company', 'property', 'aircraft', 'location')
NAME_CHARS = re.compile(r"[^\W\d_][\w.'\-]*", re.UNICODE)
ADDRESS_CHARS = re.compile(r"[\w][\w.'\-]*", re.UNICODE)
HONORIFICS = {'mr', 'mrs', 'ms', 'miss', 'dr', 'prof', 'sir', 'dame', 'lady', 'lord'}
TAIL_NUMBER = re.compile(r'\bN\d{1,5}[A-Z]{0,2}\b')
SAMPLE_LIMIT = 8  # citations per pair, preferring documents naming the fewest entities


def metadata(entity):
    value = entity.get('metadata')
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def clean_phrase(text, allow_digits=False):
    """Return a searchable multi-token phrase, or None for garbled/ambiguous labels."""
    text = ' '.join(text.replace('\xa0', ' ').split())
    if not text or any(c in text for c in '?()[]{}/\\|@#*:;"'):
        return None
    tokens = text.replace(',', ' ').split()
    pattern = ADDRESS_CHARS if allow_digits else NAME_CHARS
    if len(tokens) < 2 or any(not pattern.fullmatch(t) for t in tokens):
        return None
    # Require at least two tokens with 2+ letters ("J Tfom" is too ambiguous).
    if sum(len(t.strip(".'-")) >= 2 for t in tokens) < 2:
        return None
    return text


def entity_phrases(entity, aliases):
    kind, name = entity['entity_type'], entity['name']
    if kind == 'aircraft':
        tail = TAIL_NUMBER.search(name)
        return {tail.group()} if tail else set()
    if kind == 'person':
        if metadata(entity).get('person_type') == 'victim':
            return set()
        candidates = [name] + entity.get('original_names', [])
        if name.count(',') == 1:  # "Last, First" flight-log style.
            last, first = (p.strip() for p in name.split(','))
            candidates.append(f'{first} {last}')
        candidates += aliases.get(entity["id"], [])
    elif kind in ('property', 'location'):
        candidates = [name, name.split(',')[0]]
    else:
        candidates = [name]
    phrases = {p for c in candidates if (p := clean_phrase(c, allow_digits=kind in ('property', 'location')))}
    if kind in ('property', 'location'):
        phrases = {p for p in phrases if ',' not in p}
    return phrases


def person_tokens(name):
    if name.count(',') == 1:
        last, first = name.split(',')
        name = f'{first} {last}'
    return [t for t in re.findall(r'[^\W\d_]+', name.casefold()) if t not in HONORIFICS]


def possibly_same_entity(a, b, docs_a, docs_b):
    """Name variants of one person ("Jeffrey E. Epstein"/"Jeffrey Epstein",
    "Ms. Maxwell"/"Ghislaine Maxwell") co-occur trivially; report them as
    normalization candidates instead of connections."""
    if a['entity_type'] == b['entity_type'] == 'person':
        ta, tb = person_tokens(a['name']), person_tokens(b['name'])
        if ta and tb and ta[-1] == tb[-1]:
            if not ta[:-1] or not tb[:-1]:
                return True  # honorific/surname only
            fa, fb = sorted((ta[0], tb[0]), key=len)
            # Initial ("J"), or spelling/short form ("Glen"/"Glenn"); not Larry/Lisa.
            return (len(fa) == 1 and fb.startswith(fa)) or fb.startswith(fa)
    # Even identical document sets are not identity evidence for distinct names.
    return False


def fts_query(phrase):
    tokens = re.findall(r'\w+', phrase)
    return '"' + ' '.join(tokens) + '"'


def tokens_of(phrase):
    return tuple(re.findall(r'\w+', phrase.casefold()))


def contains(outer, inner):
    n = len(inner)
    return any(outer[i:i + n] == inner for i in range(len(outer) - n + 1))


def overlapping_terms(phrases_a, phrases_b):
    """True when one entity's phrase is contained in the other's (e.g. "Deutsche
    Bank" inside "Deutsche Bank Securities"), which makes co-mention automatic."""
    for a in map(tokens_of, phrases_a):
        for b in map(tokens_of, phrases_b):
            if contains(a, b) or contains(b, a):
                return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--view-edges', type=int, default=600,
                        help='co-mention pairs exported to the viewer (full set stays in SQLite)')
    parser.add_argument('--min-shared', type=int, default=3, help='minimum shared documents for a pair')
    parser.add_argument('--max-entities-per-doc', type=int, default=40,
                        help='documents naming more searched entities only count in shared_documents, not in the specificity score or samples')
    args = parser.parse_args()
    out = ROOT / 'output'
    if not corpus.PATH.exists():
        raise SystemExit(f'{corpus.PATH} missing; run scripts/download_corpus.py first')
    all_entities = json.loads((out / 'normalized_entities.json').read_text())
    relationships = json.loads((out / 'normalized_relationships.json').read_text())
    excluded = survivor_ids(all_entities, relationships)
    entities = [e for e in all_entities if e['entity_type'] in SEARCHED_TYPES and e['id'] not in excluded]
    registry = json.loads((ROOT / 'data/raw/persons_registry.json').read_text())
    aliases = registry_aliases(entities, registry)
    db_corpus = corpus.connect()

    started = time.time()
    # Page id -> document index, cached across runs (corpus_index.py).
    page_doc, doc_names = page_map()
    cluster = doc_clusters(doc_names)
    total_docs = len(np.unique(cluster))
    print(f'Indexed {len(page_doc) - 1:,} pages in {len(doc_names):,} documents, {total_docs:,} distinct '
          f'({time.time() - started:.1f}s)', flush=True)

    phrases, docs, terms = {}, {}, []
    skipped = Counter()
    searched = [(entity, entity_phrases(entity, aliases)) for entity in entities]
    # FTS lookups run in parallel threads (SQLite releases the GIL); identical
    # queries from different entities are run once.
    queries = sorted({fts_query(phrase) for _, entity_phrases_ in searched for phrase in entity_phrases_})
    results = dict(zip(queries, corpus.parallel_search(queries)))
    for entity, entity_phrases_ in searched:
        if not entity_phrases_:
            skipped[entity['entity_type']] += 1
            continue
        found = set()
        for phrase in sorted(entity_phrases_):
            pages = results[fts_query(phrase)]
            hits = set(page_doc[pages].tolist())
            terms.append((entity['id'], phrase, len(pages), len(hits)))
            found |= hits
        if found:
            phrases[entity['id']], docs[entity['id']] = entity_phrases_, found
    print(f'Searched {len(terms)} phrases; {len(docs)} entities found ({time.time() - started:.1f}s)', flush=True)

    # Entities with identical hit sets ("Zorro Ranch" / "Zorro Ranch, Stanley, NM")
    # are searched by the same phrase; count them once under the lowest ID.
    duplicate_of, first_with = {}, {}
    for entity_id in sorted(docs):
        if len(docs[entity_id]) < args.min_shared:
            continue
        key = frozenset(docs[entity_id])
        # Only collapse when the same phrase was searched; distinct names that
        # happen to share the same few documents stay separate.
        if key in first_with and overlapping_terms(phrases[entity_id], phrases[first_with[key]]):
            duplicate_of[entity_id] = first_with[key]
        else:
            first_with.setdefault(key, entity_id)
    # Invert to document -> entities, then count pairs per document.
    doc_entities = defaultdict(list)
    for entity_id, hits in docs.items():
        if entity_id in duplicate_of:
            continue
        for d in hits:
            doc_entities[d].append(entity_id)
    for members in doc_entities.values():
        members.sort()
    shared, weighted, samples = cluster_pair_counts(
        doc_entities, lambda d: int(cluster[d]), lambda members: combinations(members, 2), args.max_entities_per_doc)
    # Distinct documents per entity (copies counted once), for lift.
    distinct = {entity_id: len(np.unique(cluster[np.fromiter(hits, dtype=np.int64, count=len(hits))])) for entity_id, hits in docs.items()}
    print(f'Counted {len(shared):,} co-mentioned pairs ({time.time() - started:.1f}s)', flush=True)

    by_id = {e['id']: e for e in entities}
    # Existing links come from current inputs, not a previous combined build.
    existing = set()
    for name in ('normalized_relationships.json', 'communication_edges.json'):
        path = out / name
        if path.exists():
            existing.update(frozenset((r['source_entity_id'], r['target_entity_id'])) for r in json.loads(path.read_text()))
    communication_db = out / 'communication_graph.db'
    if communication_db.exists():
        # Direct header links between graph entities in the FULL metadata graph,
        # not only the bounded viewer slice.
        with closing(sqlite3.connect(f'file:{communication_db}?mode=ro', uri=True)) as con:
            for a, b in con.execute("SELECT source_id, target_id FROM edges WHERE source_id LIKE 'kg:%' AND target_id LIKE 'kg:%'"):
                existing.add(frozenset((int(a[3:]), int(b[3:]))))
    candidates = [(a, b, count) for (a, b), count in shared.items()
                  if count >= args.min_shared and not overlapping_terms(phrases[a], phrases[b])]
    identity_candidates = [(a, b, count) for a, b, count in candidates
                           if possibly_same_entity(by_id[a], by_id[b], docs[a], docs[b])]
    # A link to a name variant counts as an existing link ("Jeffrey E. Epstein"
    # is not newly connected to someone already linked to "Jeffrey Epstein").
    # Only strong variants (both have compatible given names) propagate links;
    # honorific/surname-only candidates ("Ms. Maxwell") stay advisory.
    variants = defaultdict(set)
    for a, b, _ in identity_candidates:
        if by_id[a]['entity_type'] == 'person' and all(len(person_tokens(by_id[i]['name'])) >= 2 for i in (a, b)):
            variants[a].add(b)
            variants[b].add(a)
    for duplicate, kept in duplicate_of.items():
        variants[kept].add(duplicate)

    def linked(a, b):
        return any(frozenset((x, y)) in existing for x in variants[a] | {a} for y in variants[b] | {b})

    skip = {(a, b) for a, b, _ in identity_candidates}
    pairs = []
    for a, b, count in candidates:
        if (a, b) in skip:
            continue
        lift = count * total_docs / (distinct[a] * distinct[b])
        sample = [doc_names[d] for _, d in sorted(samples[a, b])[:SAMPLE_LIMIT]]
        pairs.append({'a': a, 'b': b, 'shared_documents': count, 'specificity_score': round(weighted[a, b], 3),
                      'lift': round(lift, 2), 'docs_a': distinct[a], 'docs_b': distinct[b],
                      'already_linked': linked(a, b), 'sample_eftas': sample})
    # Rank by specificity (shared small documents) and penalize pairs that are
    # merely two very frequent names (lift < 1 means co-mention at or below chance).
    pairs.sort(key=lambda p: (-(p['specificity_score'] * min(p['lift'], 10) ** 0.5), p['a'], p['b']))

    # Full evidence to SQLite for SQL follow-up.
    db_path = out / 'comention_graph.partial.db'
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript('''
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE entities(id INTEGER PRIMARY KEY, name TEXT, entity_type TEXT, document_count INTEGER,
                              distinct_documents INTEGER);
        CREATE TABLE search_terms(entity_id INTEGER, phrase TEXT, page_hits INTEGER, document_hits INTEGER);
        CREATE TABLE mentions(entity_id INTEGER, efta_number TEXT);
        CREATE TABLE pairs(a INTEGER, b INTEGER, shared_documents INTEGER, specificity_score REAL, lift REAL,
                           already_linked INTEGER, sample_eftas TEXT);
    ''')
    db.executemany('INSERT INTO entities VALUES (?,?,?,?,?)', [(i, by_id[i]['name'], by_id[i]['entity_type'], len(d), distinct[i]) for i, d in docs.items()])
    db.executemany('INSERT INTO search_terms VALUES (?,?,?,?)', terms)
    db.executemany('INSERT INTO mentions VALUES (?,?)', ((i, doc_names[d]) for i, hits in docs.items() for d in hits))
    db.executemany('INSERT INTO pairs VALUES (?,?,?,?,?,?,?)', [(p['a'], p['b'], p['shared_documents'], p['specificity_score'], p['lift'], p['already_linked'], json.dumps(p['sample_eftas'])) for p in pairs])
    db.executescript('''
        CREATE INDEX mentions_entity ON mentions(entity_id, efta_number);
        CREATE INDEX mentions_efta ON mentions(efta_number);
        CREATE INDEX pairs_a ON pairs(a); CREATE INDEX pairs_b ON pairs(b);
    ''')
    db.commit()
    db.close()
    db_path.replace(out / 'comention_graph.db')

    wanted = sorted({e for p in pairs[:args.view_edges] for e in p['sample_eftas']})
    urls = {}
    for i in range(0, len(wanted), 500):
        chunk = wanted[i:i + 500]
        urls.update(db_corpus.execute(f"SELECT id, url FROM documents WHERE url IS NOT NULL AND id IN ({','.join('?' * len(chunk))})", chunk))
    db_corpus.close()
    # Official URLs for every DOJ document; corpus pdf_url covers only ~1%.
    urls = {**corpus.document_urls(wanted), **urls}
    view_edges = []
    for p in pairs[:args.view_edges]:
        view_edges.append({
            'id': f'comention_{p["a"]}_{p["b"]}', 'source_entity_id': p['a'], 'target_entity_id': p['b'],
            'relationship_type': 'co_mentioned_in_documents', 'weight': p['shared_documents'],
            'date_first': None, 'date_last': None,
            'metadata': {'meaning': 'Both names appear as exact phrases in the same EFTA documents (near-duplicate copies counted once). A reading lead, NOT evidence of a relationship, meeting or wrongdoing.',
                         'new_link': not p['already_linked'],
                         'shared_documents': p['shared_documents'], 'specificity_score': p['specificity_score'],
                         'lift_vs_chance': p['lift'], 'documents_mentioning_source': p['docs_a'], 'documents_mentioning_target': p['docs_b'],
                         'sample_documents': [{'efta_number': e, **({'pdf_url': urls[e]} if e in urls else {})} for e in p['sample_eftas']],
                         'search_phrases': {'source': sorted(phrases[p['a']]), 'target': sorted(phrases[p['b']])},
                         'complete_evidence': 'output/comention_graph.db (mentions table); scripts/cooccur.py for page snippets'}})
    (out / 'comention_edges.json').write_text(json.dumps(view_edges, indent=2) + '\n')
    with (out / 'new_connections.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['rank', 'entity_a', 'type_a', 'entity_b', 'type_b', 'shared_documents', 'specificity_score',
                         'lift_vs_chance', 'docs_a', 'docs_b', 'sample_eftas'])
        rank = 0
        for p in pairs:
            if p['already_linked']:
                continue
            rank += 1
            a, b = by_id[p['a']], by_id[p['b']]
            writer.writerow([rank, a['name'], a['entity_type'], b['name'], b['entity_type'], p['shared_documents'],
                             p['specificity_score'], p['lift'], p['docs_a'], p['docs_b'], ' '.join(p['sample_eftas'])])
    with (out / 'identity_candidates.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['entity_a_id', 'entity_a', 'entity_b_id', 'entity_b', 'entity_type', 'shared_documents', 'reason'])
        for a, b, count in sorted(identity_candidates, key=lambda c: -c[2]):
            writer.writerow([a, by_id[a]['name'], b, by_id[b]['name'], by_id[a]['entity_type'], count,
                             'compatible person name variant; review before merging'])
        for duplicate, kept in sorted(duplicate_of.items()):
            writer.writerow([kept, by_id[kept]['name'], duplicate, by_id[duplicate]['name'], by_id[kept]['entity_type'],
                             len(docs[kept]), 'identical search hits; co-mention counted under the first ID'])
    summary = {'documents_indexed': total_docs, 'entities_searched': len(by_id) - sum(skipped.values()),
               'entities_with_hits': len(docs), 'entities_skipped_unsafe_or_ambiguous_label': dict(skipped),
               'survivors_excluded': len(excluded),
               'registry_aliases_used': sum(map(len, aliases.values())), 'search_phrases': len(terms),
               'pairs_min_shared': len(pairs), 'identity_candidates': len(identity_candidates) + len(duplicate_of), 'pairs_new': sum(not p['already_linked'] for p in pairs),
               'viewer_edges': len(view_edges), 'min_shared_documents': args.min_shared,
               'seconds': round(time.time() - started, 1),
               'warning': 'Co-mention is a reading lead only. Exact-phrase matching misses spelling variants and over-matches common names/OCR noise. Configured survivors, victim-labelled entities and victim_of sources are not searched.'}
    (out / 'comention_summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
