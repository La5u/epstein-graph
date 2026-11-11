"""Censored upstream victim designators as graph nodes, with document links.

The DOJ release and the upstream exports anonymize victims: the corpus refers
to them only by pseudonymous designators ("Jane Doe #2", "Minor Victim-3",
"Anonymous Victim"). This step promotes those designators to separate 'survivor'
entities and links each one to the graph people/entities it shares EFTA
documents with, so participant<->victim paths can be read in the viewer.

Only the censored pseudonym is ever used. No redacted text is read, and no
pseudonym is resolved to a real identity.

    .venv/bin/python scripts/build_victims.py [--view-edges 800]

Outputs:
  output/victim_nodes.json   one node per designator, with document count and
                             sample EFTA references.
  output/victim_edges.json   designator <-> entity document co-mention edges
                             (a reading lead, not a relationship).
"""
import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import corpus

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'

# Stem phrase -> regex capturing the designator number ('Jane Doe #2', 'Jane Doe No. 2',
# 'Jane Doe 410'); a number running on ('Jane Doe 2,000') is not a designator.
PATTERNS = (
    ('Jane Doe', re.compile(r'Jane\s+Does?\s*(?:#|No\.?|Number)?\s*(\d{1,3})\b(?![,.]?\d)', re.I)),
    ('John Doe', re.compile(r'John\s+Does?\s*(?:#|No\.?|Number)?\s*(\d{1,3})\b(?![,.]?\d)', re.I)),
    ('Minor Victim', re.compile(r'Minor\s+Victims?\s*[-#]?\s*(\d{1,3})\b(?![,.]?\d)', re.I)),
)
MEANING = ('Censored upstream victim designator and graph entity share EFTA documents; '
           'a same-document reading lead, not a relationship, contact or offence. '
           'The designator is a pseudonym; no victim identity is resolved.')


# Where a designator is used: the first matching context on the page. Numbers
# restart in every matter, so "Jane Doe #2" in the CVRA case and in a 2008
# civil suit can be different people; the breakdown is shown, never resolved.
CONTEXTS = (
    ('CVRA case (Jane Doe v. United States)', re.compile(r'crime victims|\bcvra\b|v\.?\s+united states|08-80736', re.I)),
    ('estate / compensation claims', re.compile(r"creditor'?s claim|probate|estate of jeffrey|compensation program", re.I)),
    ('Maxwell prosecution', re.compile(r'maxwell|20 cr\.? 330', re.I)),
    ('JPMorgan / Deutsche Bank suits', re.compile(r'jp ?morgan|deutsche bank', re.I)),
    ('criminal investigation / draft indictment', re.compile(r'indictment|grand jury|knowingly persuade|palm beach police|victim notification', re.I)),
    ('civil suits against Epstein', re.compile(r'vs?\.?\s+(?:jeffrey\s+)?epstein|08-cv-80|08-80\d{3}|17 civ', re.I)),
)


def context_of(text):
    return next((label for label, pattern in CONTEXTS if pattern.search(text or '')), 'unspecified')


CITATION = re.compile(r'(?:\bv|\bvs)\.?\s*$', re.I)


def designators_in_text(text):
    """{canonical designator name: hit count} for one page of text.

    Skips "Doherty v. John Doe No. 22"-style case citations: the designator is a
    precedenting case name there, not a party. The plaintiff form ("Jane Doe No.
    102 v. Epstein") is kept because the designator precedes the "v.".
    """
    found = Counter()
    for stem, pattern in PATTERNS:
        for match in pattern.finditer(text or ''):
            if CITATION.search((text or '')[max(0, match.start() - 4):match.start()]):
                continue
            number = int(match.group(1))
            if number == 0:
                continue  # OCR of 'No. 6'/'#8' etc.; no matter numbers its victims from 0
            name = f'Minor Victim-{number}' if stem == 'Minor Victim' else f'{stem} #{number}'
            found[name] += 1
    return found


def slug(name):
    return 'victim_' + re.sub(r'\W+', '_', name.casefold()).strip('_')


def collect_designator_docs(db, contexts=None):
    """{designator: set(efta_number)} over the whole corpus; contexts, if given,
    collects {designator: Counter(context label per page)}."""
    docs = defaultdict(set)
    for stem, _ in PATTERNS:
        for efta, text in corpus.search_text(db, f'"{stem}" OR "{stem}s"'):
            found = designators_in_text(text)
            for name in found:
                docs[name].add(efta)
                if contexts is not None:
                    contexts.setdefault(name, Counter())[context_of(text)] += 1
    return docs


def load_person_docs(path):
    """{entity_id: (name, entity_type, set(efta))} from the co-mention database."""
    people = {}
    if not Path(path).exists():
        return people
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        meta = {i: (name, kind) for i, name, kind in
                db.execute('SELECT id, name, entity_type FROM entities')}
        for entity_id, efta in db.execute('SELECT entity_id, efta_number FROM mentions'):
            entry = people.get(entity_id)
            if entry is None:
                entry = people[entity_id] = (meta.get(entity_id, ('', ''))[0],
                                             meta.get(entity_id, ('', ''))[1], set())
            entry[2].add(efta)
    return people


def crowding_score(shared, names_in):
    """Sum over shared documents of 1 / (names in the document - 1): a document
    naming only the two counts 1, a compilation naming 50 graph names ~0.02.
    Same weighting as the co-mention layer's specificity_score."""
    return sum(1 / max(names_in[d] - 1, 1) for d in shared)


def build(corpus_path=corpus.PATH, comention_path=None, min_docs=1, min_shared=3,
          per_victim=25, view_edges=800, relevance_docs=None, min_score=1.0):
    comention_path = Path(comention_path) if comention_path else OUT / 'comention_graph.db'
    people = load_person_docs(comention_path)
    epstein = {entity_id for entity_id, (name, kind, _) in people.items()
               if name.strip().casefold() == 'jeffrey epstein' and kind == 'person'}
    reference = set().union(*(people[i][2] for i in epstein)) if epstein else None

    db = corpus.connect(Path(corpus_path)) if Path(corpus_path).exists() else None
    contexts = {}
    designators = collect_designator_docs(db, contexts) if db else {}
    if db:
        db.close()

    # Names per document: graph names (co-mention search) plus designators.
    names_in = Counter()
    for _, _, entity_docs in people.values():
        names_in.update(entity_docs)
    for docs in designators.values():
        names_in.update(docs)

    nodes, candidates, crowded, documents = [], [], 0, {}
    for name in sorted(designators, key=lambda n: (-len(designators[n]), n)):
        docs = designators[name]
        if len(docs) < min_docs:
            continue
        if reference is not None and not (docs & reference):
            continue  # designator from an unrelated matter, not this corpus
        node_id = slug(name)
        documents[node_id] = sorted(docs)
        seen_in = contexts.get(name, Counter())
        nodes.append({
            'id': node_id, 'name': name, 'entity_type': 'survivor',
            'normalized_name': ' '.join(name.casefold().split()),
            'document_count': len(docs), 'sample_documents': sorted(docs)[:8],
            'designator': True,
            'pages_by_context': dict(seen_in.most_common()),
            'note': ('Censored upstream pseudonym from the corpus. Promoted to a '
                     'survivor node so participant<->survivor document links are visible; '
                     'it identifies no person. Numbers are per-matter labels, not a count: '
                     'the same number in two cases can be two different people (see pages_by_context).'),
        })
        shared = []
        for entity_id, (_, kind, entity_docs) in people.items():
            if entity_id in epstein:
                continue
            together = docs & entity_docs
            if len(together) < min_shared:
                continue
            score = crowding_score(together, names_in)
            if score < min_score:
                crowded += 1
                continue
            # Least crowded documents first: the most specific evidence to read.
            samples = sorted(together, key=lambda d: (names_in[d], d))[:5]
            shared.append((score, len(together), entity_id, samples))
        shared.sort(key=lambda item: (-item[0], str(item[2])))
        candidates.extend((score, count, node_id, entity_id, samples)
                          for score, count, entity_id, samples in shared[:per_victim])
    candidates.sort(key=lambda item: (-item[0], item[2], str(item[3])))

    edges = []
    for score, count, node_id, entity_id, samples in candidates[:view_edges]:
        edges.append({'id': f'victimlink_{node_id}_{entity_id}',
                      'source_entity_id': node_id, 'target_entity_id': entity_id,
                      # Drawn by specificity, not raw counts: big compilations add little.
                      'relationship_type': 'victim_document_co_mention', 'weight': round(score, 2),
                      'date_first': None, 'date_last': None,
                      'metadata': {'shared_documents': count, 'specificity_score': round(score, 2),
                                   'sample_eftas': samples, 'designator': True, 'meaning': MEANING}})
    summary = {'designators': len(nodes), 'designators_found': len(designators),
               'by_stem': dict(Counter(n['name'].split(' #')[0].split('-')[0] for n in nodes)), 'document_links': len(edges),
               'candidate_links': len(candidates), 'min_docs': min_docs,
               'min_shared': min_shared, 'min_specificity_score': min_score,
               'dropped_as_only_crowded_documents': crowded,
               'relevance_checked': reference is not None,
               'note': 'Censored pseudonyms only; no victim identity is resolved. '
                       'Links are same-document reading leads, not relationships.'}
    return nodes, edges, summary, documents


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--view-edges', type=int, default=800)
    parser.add_argument('--min-docs', type=int, default=1)
    parser.add_argument('--min-shared', type=int, default=3)
    parser.add_argument('--per-victim', type=int, default=25)
    parser.add_argument('--min-score', type=float, default=1.0,
                        help='minimum specificity score: shared documents weighted 1/(names in document - 1)')
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    if not corpus.PATH.exists():
        for name in ('victim_nodes.json', 'victim_edges.json', 'victim_summary.json', 'victim_documents.json'):
            (OUT / name).write_text('[]\n')
        print('corpus missing; wrote empty victim outputs')
        return
    nodes, edges, summary, documents = build(min_docs=args.min_docs, min_shared=args.min_shared,
                                  per_victim=args.per_victim, view_edges=args.view_edges,
                                  min_score=args.min_score)
    (OUT / 'victim_nodes.json').write_text(json.dumps(nodes, indent=2) + '\n')
    (OUT / 'victim_edges.json').write_text(json.dumps(edges, indent=2) + '\n')
    (OUT / 'victim_summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    (OUT / 'victim_documents.json').write_text(json.dumps(documents) + '\n')  # every document per designator
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
