"""Rank possible duplicate graph entities for review.

    .venv/bin/python scripts/find_duplicates.py   -> output/duplicate_candidates.csv

Rules (each candidate lists every rule that fired):
  initial      "J Tfom" / "Jane Teal"; "Dfoken K. Indyke" / "DARREN INDYKE"
  surname_only "Dubin" / "Glenn Dubin" (ambiguous when several full names share it)
  spelling     edit distance <= 2 on full names ("Glen Dubin" / "Glenn Dubin")
  punctuation  same letters ignoring spaces/hyphens/order ("Jean-Luc" / "Jean Luc")
  alias        upstream KG or persons-registry alias
Evidence: shared neighbours (Jaccard), shared corpus documents, and whether
they appear on the same flight (two names on one manifest usually means two
different passengers). Candidates are NOT merged here; reviewed decisions go
into config/identity_merges.json.
"""
import csv
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from contextlib import closing
from itertools import combinations
from pathlib import Path

from normalize_graph import clean_display_name, name_key

ROOT = Path(__file__).resolve().parents[1]
HONORIFICS = {'mr', 'mrs', 'ms', 'miss', 'dr', 'prof', 'sir', 'dame', 'lady', 'lord'}
SUFFIXES = {'jr', 'sr', 'ii', 'iii', 'iv', 'pilot', 'esq'}


def words(name):
    name = unicodedata.normalize('NFKD', clean_display_name(name))
    name = ''.join(c for c in name if not unicodedata.combining(c))
    return [w for w in re.findall(r'[a-z]+', name.casefold()) if w not in HONORIFICS and w not in SUFFIXES]


def edit_distance(a, b, limit=2):
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        previous, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (ca != cb))
    return row[-1]


def rules_for(a, b, wa, wb):
    rules = []
    if not wa or not wb:
        return rules
    if sorted(''.join(wa)) == sorted(''.join(wb)) and ''.join(sorted(wa)) == ''.join(sorted(wb)):
        rules.append('punctuation')
    if wa[-1] == wb[-1] or (len(wa) > 1 and wa[0] == wb[-1]) or (len(wb) > 1 and wb[0] == wa[-1]):
        short, long_ = sorted((wa, wb), key=len)
        if len(short) == 1:
            rules.append('surname_only')
        elif short[0][0] == long_[0][0] and (len(short[0]) == 1 or len(long_[0]) == 1 or short[0] == long_[0]) and wa != wb:
            rules.append('initial')
    if len(wa) > 1 and len(wb) > 1 and ' '.join(wa) != ' '.join(wb) and edit_distance(' '.join(wa), ' '.join(wb)) <= 2:
        rules.append('spelling')
    return rules


def main():
    out = ROOT / 'output'
    entities = [e for e in json.loads((out / 'normalized_entities.json').read_text()) if e['entity_type'] == 'person']
    relationships = json.loads((out / 'graph_relationships.json').read_text())
    neighbours, flights = defaultdict(set), set()
    for r in relationships:
        a, b = r['source_entity_id'], r['target_entity_id']
        neighbours[a].add(b)
        neighbours[b].add(a)
        if r['relationship_type'] == 'traveled_with':
            flights.add(frozenset((a, b)))
    docs = defaultdict(set)
    comention = out / 'comention_graph.db'
    if comention.exists():
        with closing(sqlite3.connect(f'file:{comention}?mode=ro', uri=True)) as db:
            for entity_id, efta in db.execute('SELECT entity_id, efta_number FROM mentions WHERE entity_id IN (SELECT id FROM entities WHERE document_count < 200000)'):
                docs[entity_id].add(efta)
    aliases = defaultdict(set)
    for e in entities:
        raw = e.get('aliases')
        for alias in (json.loads(raw) if isinstance(raw, str) else raw or []):
            aliases[e['id']].add(name_key(alias))
    by_surname = defaultdict(list)
    tokens = {e['id']: words(e['name']) for e in entities}
    for e in entities:
        for w in set(tokens[e['id']][-1:] + tokens[e['id']][:1]):
            by_surname[w].append(e)
    by_id = {e['id']: e for e in entities}
    rows, seen = [], set()
    candidates = set()
    for group in by_surname.values():
        candidates.update(frozenset((a['id'], b['id'])) for a, b in combinations(group, 2))
    # Spelling variants need not share a surname token ("Rodriquez").
    full = [e for e in entities if len(tokens[e['id']]) > 1]
    for a, b in combinations(full, 2):
        if abs(len(a['normalized_name']) - len(b['normalized_name'])) <= 2 and edit_distance(' '.join(tokens[a['id']]), ' '.join(tokens[b['id']])) <= 2:
            candidates.add(frozenset((a['id'], b['id'])))
    surname_full_names = defaultdict(set)
    for e in entities:
        if len(tokens[e['id']]) > 1:
            surname_full_names[tokens[e['id']][-1]].add(e['id'])
    for pair in candidates:
        a, b = sorted(pair)
        if pair in seen:
            continue
        seen.add(pair)
        ea, eb = by_id[a], by_id[b]
        rules = rules_for(ea, eb, tokens[a], tokens[b])
        if name_key(eb['name']) in aliases[a] or name_key(ea['name']) in aliases[b]:
            rules.append('alias')
        if not rules:
            continue
        shared = len(neighbours[a] & neighbours[b] - {a, b})
        union = len(neighbours[a] | neighbours[b] - {a, b}) or 1
        both_docs = len(docs[a] & docs[b])
        ambiguous = ''
        if 'surname_only' in rules:
            short = a if len(tokens[a]) == 1 else b
            options = surname_full_names[tokens[short][0]]
            if len(options) > 1:
                ambiguous = f'{len(options)} full names share this surname'
        rows.append({'entity_a_id': a, 'entity_a': ea['name'], 'entity_b_id': b, 'entity_b': eb['name'],
                     'rules': ' '.join(rules), 'shared_neighbours': shared, 'neighbour_jaccard': round(shared / union, 2),
                     'shared_documents': both_docs, 'same_flight': pair in flights, 'ambiguous': ambiguous})
    rows.sort(key=lambda r: (r['same_flight'], bool(r['ambiguous']), -r['neighbour_jaccard'], -r['shared_documents']))
    with (out / 'duplicate_candidates.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ['entity_a_id'])
        writer.writeheader()
        writer.writerows(rows)
    print(f'{len(rows)} candidate pairs -> output/duplicate_candidates.csv')


if __name__ == '__main__':
    main()
