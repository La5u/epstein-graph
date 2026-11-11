"""Reviewed corpus vehicles and document co-mentions with graph people/places.

Discovery uses batched FTS queries, streaming matching pages only. Regex candidates
are reading leads, never automatic nodes: config/vehicles.json is the review gate.
New nodes and edges require two distinct documents (near-duplicates count once).
Existing KG aircraft keep their IDs even when the corpus cannot corroborate them.
Names need nearby vehicle vocabulary; bare ambiguous words do not count. This is
conservative lexical evidence, not an ownership, passenger or visit inference.
"""
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import numpy as np

import corpus
from build_comentions import entity_phrases, fts_query, metadata, overlapping_terms
from corpus_index import canonical_documents, cluster_pair_counts, page_map
from normalize_graph import name_key, registry_aliases
from survivor_policy import survivor_ids

ROOT = Path(__file__).resolve().parents[1]
MIN_DOCUMENTS = 2
SAMPLE_LIMIT = 8
CONTEXT = 240
TARGET_TYPES = {'person', 'property', 'location'}
CUES = {
    'boat': re.compile(r'\b(?:boats?|vessels?|yachts?|tenders?|ferr(?:y|ies)|barge|catamaran|ribtec|procat|nautica|sportis|cape horn|midship marine|donzi|pirogue|sea doo|zodiac|caribe|uscg|hull)\b|\b[MS][/.]\s*Y\b', re.I),
    'aircraft': re.compile(r'\b(?:aircraft|airplanes?|planes?|helicopters?|gulfstream|cessna|boeing|sikorsky|bell|aviation|airframe|tail\s*(?:number|no)|flight\s*logs?)\b', re.I),
    'car': re.compile(r'\b(?:cars?|automobiles?|vehicles?|VIN|license plate)\b', re.I),
}
DISCOVERY_TERMS = ('yacht', 'yachts', 'motor yacht', 'M Y', 'S Y', 'boat', 'boats',
                   'vessel', 'tender', 'ferry', 'barge', 'USCG', 'hull', 'aircraft',
                   'helicopter', 'gulfstream', 'cessna', 'boeing', 'sikorsky')
TAIL = re.compile(r'\bN[1-9](?:\d{0,4}|\d{0,3}[A-HJ-NP-Z]|\d{0,2}[A-HJ-NP-Z]{2})\b', re.I)
BOAT_NAME = re.compile(
    r'\b(?:[MS][/.]\s*Y|[Mm]otor [Yy]acht|[Yy]acht|[Vv]essel|[Bb]oat|[Tt]ender|[Ff]erry)'
    r'[\s,:-]+(?:(?i:named|called|name)\s*[:\-]?\s*)?["“\']?'
    r'([A-Z][A-Za-z\'-]*(?:\s+[A-Z][A-Za-z\'-]*){0,3}(?:\s+(?:II|III|IV))?)')
BOAT_ID = re.compile(r'\b(?:USCG\s*(?:Doc\.?\s*)?(?:No\.?|number)?\s*[:#]?\s*(\d{5,8})'
                     r'|(?:hull (?:identification |ID )?(?:number|no\.?)\s*[:#]?\s*)([A-Z0-9-]{8,17}))\b', re.I)


def phrase_pattern(phrase):
    return re.compile(r'(?<!\w)' + r'[\W_]+'.join(map(re.escape, re.findall(r'\w+', phrase))) + r'(?!\w)', re.I)


def context_at(text, start, end):
    return text[max(0, start - CONTEXT):end + CONTEXT]


def qualified_matches(text, pattern, kind):
    """Yield every contextual match, not just a first occurrence in a header."""
    for match in pattern.finditer(text):
        snippet = context_at(text, match.start(), match.end())
        if CUES[kind].search(snippet):
            yield snippet


def sample_key(doc):
    return (not bool(re.fullmatch(r'EFTA\d{8}', doc)), doc)


def remember_sample(samples, doc, snippet, canonical=None):
    # Bound excerpts during streaming, including for noisy discovery candidates.
    canonical = canonical or {}
    cluster = canonical.get(doc, doc)
    for old in list(samples):
        if old != doc and canonical.get(old, old) == cluster:
            if sample_key(old) < sample_key(doc):
                return
            del samples[old]
    if doc in samples:
        samples[doc] = min(samples[doc], snippet)
    elif len(samples) < SAMPLE_LIMIT or sample_key(doc) < max(map(sample_key, samples)):
        samples[doc] = snippet
        if len(samples) > SAMPLE_LIMIT:
            del samples[max(samples, key=sample_key)]


def distinct_count(documents, canonical):
    return len({canonical.get(d, d) for d in documents})


def discover(db, canonical):
    docs, samples = defaultdict(set), defaultdict(dict)
    pages = 0
    for doc, text in corpus.search_text(db, ' OR '.join(map(fts_query, DISCOVERY_TERMS))):
        pages += 1
        text = ' '.join((text or '').split())
        found = []
        for match in TAIL.finditer(text):
            snippet = context_at(text, match.start(), match.end())
            if CUES['aircraft'].search(snippet):
                found.append(('aircraft', match.group().upper(), snippet))
        for match in BOAT_NAME.finditer(text):
            found.append(('boat', match[1].strip("'- "), context_at(text, match.start(), match.end())))
        for match in BOAT_ID.finditer(text):
            found.append(('boat', (match[1] or match[2]).upper(), context_at(text, match.start(), match.end())))
        for kind, name, snippet in found:
            key = kind, name_key(name)
            docs[key].add(doc)
            remember_sample(samples[key], doc, snippet, canonical)
    candidates = [{'name': name.upper() if kind == 'aircraft' else name, 'kind': kind, 'document_count': len(hits),
                   'distinct_documents': distinct_count(hits, canonical), 'reviewed': False,
                   'sample_eftas': sorted(samples[kind, name]),
                   'samples': [{'efta_number': d, 'snippet': s} for d, s in sorted(samples[kind, name].items())]}
                  for (kind, name), hits in docs.items()]
    candidates.sort(key=lambda c: (-c['distinct_documents'], c['kind'], c['name']))
    return candidates, pages


def search_phrases(spec):
    return sorted({p for p in [spec['name'], *spec['aliases'], spec.get('registration')]
                   if p and re.findall(r'\w+', p)})


def load_review(path):
    specs = json.loads(path.read_text())['vehicles']
    used = set()
    for spec in specs:
        if spec['kind'] not in CUES or not spec['name'].strip() or not isinstance(spec['aliases'], list):
            raise ValueError(f'Invalid vehicle: {spec!r}')
        if not spec.get('note') or not isinstance(spec.get('sample_eftas'), list):
            raise ValueError(f'Vehicle needs a review note and sample_eftas: {spec["name"]}')
        if not spec.get('kg_id') and len(set(spec['sample_eftas'])) < MIN_DOCUMENTS:
            raise ValueError(f'New vehicle needs at least two reviewed citations: {spec["name"]}')
        for phrase in search_phrases(spec):
            key = spec['kind'], fts_query(phrase).casefold()
            if key in used:
                raise ValueError(f'Duplicate vehicle alias: {phrase}')
        used.update((spec['kind'], fts_query(p).casefold()) for p in search_phrases(spec))
    return specs


def reviewed_mentions(db, specs, canonical=None):
    docs, samples = defaultdict(set), defaultdict(dict)
    owners = defaultdict(list)
    for i, spec in enumerate(specs):
        for phrase in search_phrases(spec):
            owners[fts_query(phrase)].append((i, phrase_pattern(phrase)))
    queries = sorted(owners)
    for offset in range(0, len(queries), 64):
        chunk = queries[offset:offset + 64]
        patterns = [owner for q in chunk for owner in owners[q]]
        for doc, text in corpus.search_text(db, ' OR '.join(chunk)):
            text = ' '.join((text or '').split())
            for i, pattern in patterns:
                for snippet in qualified_matches(text, pattern, specs[i]['kind']):
                    docs[i].add(doc)
                    remember_sample(samples[i], doc, snippet, canonical)
    return docs, samples


def vehicle_id(spec):
    key = spec.get('registration') or name_key(spec['name'])
    return 'vehicle_' + spec['kind'] + '_' + hashlib.sha1(key.encode()).hexdigest()[:16]


def make_nodes(specs, docs, samples, entities, canonical):
    kg = {e['id']: e for e in entities if e['entity_type'] == 'aircraft'}
    by_tail = {m.group().upper(): e['id'] for e in kg.values() if (m := TAIL.search(e['name']))}
    nodes, mentions, phrases, rejected = {}, {}, {}, []
    for i, spec in enumerate(specs):
        existing = spec.get('kg_id')
        if existing is not None and existing not in kg:
            raise ValueError(f'Unknown KG aircraft ID: {existing}')
        if existing is None and spec['kind'] == 'aircraft':
            existing = by_tail.get(spec.get('registration'))
        count = distinct_count(docs[i], canonical)
        if count < MIN_DOCUMENTS and existing is None:
            rejected.append({'name': spec['name'], 'distinct_documents': count})
            continue
        node_id = existing if existing is not None else vehicle_id(spec)
        if node_id in nodes:
            raise ValueError(f'Two reviews resolve to vehicle {node_id}')
        base = kg.get(node_id, {})
        nodes[node_id] = {**base, 'id': node_id, 'name': spec['name'], 'entity_type': 'vehicle',
                          'vehicle_type': spec['kind'], 'aliases': spec['aliases'],
                          'registration': spec.get('registration'), 'note': spec['note'],
                          'source': 'Reviewed corpus evidence: config/vehicles.json',
                          'metadata': {**metadata(base), 'document_count': len(docs[i]),
                                       'distinct_documents': count, 'corpus_corroborated': count >= MIN_DOCUMENTS,
                                       'reviewed_sample_eftas': spec['sample_eftas'],
                                       'sample_eftas': sorted(samples[i], key=sample_key),
                                       'complete_evidence': 'output/vehicle_evidence.json',
                                       **({'original_kg_name': base['name'], 'original_kg_metadata': metadata(base)} if base else {}),
                                       **({'aircraft_type': spec['model']} if spec.get('model') else {})}}
        for field in ('hull_number', 'serial_number', 'registration_note'):
            if field in spec:
                nodes[node_id][field] = spec[field]
        mentions[node_id], phrases[node_id] = docs[i], set(search_phrases(spec))
    # Retyping existing nodes is unconditional; evidence is never invented.
    for node_id, entity in kg.items():
        if node_id not in nodes:
            nodes[node_id] = {**entity, 'entity_type': 'vehicle', 'vehicle_type': 'aircraft',
                              'metadata': {**metadata(entity), 'corpus_corroborated': False,
                                           'document_count': 0, 'distinct_documents': 0}}
            mentions[node_id], phrases[node_id] = set(), set()
    return list(nodes.values()), mentions, phrases, rejected


def target_mentions(entities, aliases, page_doc, doc_names, path):
    phrases = {e['id']: entity_phrases(e, aliases) for e in entities}
    docs = defaultdict(set)
    owners = defaultdict(list)
    for entity_id, terms in phrases.items():
        if terms:
            owners[' OR '.join(sorted({fts_query(p) for p in terms}))].append(entity_id)
    # One bounded result at a time; batch each entity's aliases in one FTS query.
    with closing(corpus.connect(path)) as db:
        for query in sorted(owners):
            pages = corpus.search(db, query)
            hits = {doc_names[d] for d in np.unique(page_doc[pages]) if d >= 0}
            for entity_id in owners[query]:
                docs[entity_id].update(hits)
    return docs, phrases


def make_edges(vehicle_docs, target_docs, phrases, canonical, total_documents, existing=()):
    members = defaultdict(list)
    wanted = set().union(*vehicle_docs.values()) if vehicle_docs else set()
    for entity_id, docs in {**vehicle_docs, **target_docs}.items():
        for d in docs & wanted:
            members[d].append(entity_id)
    for values in members.values():
        values.sort(key=str)

    def pairs(values):
        for a in values:
            if a in vehicle_docs:
                for b in values:
                    if b in target_docs:
                        yield a, b

    shared, weighted, samples = cluster_pair_counts(members, lambda d: canonical.get(d, d), pairs, 40)
    counts = {i: distinct_count(d, canonical) for i, d in {**vehicle_docs, **target_docs}.items()}
    linked = {frozenset((e['source_entity_id'], e['target_entity_id'])) for e in existing}
    edges = []
    for (a, b), count in sorted(shared.items(), key=lambda row: (str(row[0][0]), str(row[0][1]))):
        if count < MIN_DOCUMENTS or overlapping_terms(phrases[a], phrases[b]):
            continue
        # Always cite actual documents containing both ends, even long filings.
        sample = [d for _, d in sorted(samples[a, b])[:SAMPLE_LIMIT]]
        if not sample:
            common = sorted(vehicle_docs[a] & target_docs[b])
            seen = set()
            for d in common:
                cluster = canonical.get(d, d)
                if cluster not in seen:
                    sample.append(d)
                    seen.add(cluster)
                if len(sample) == SAMPLE_LIMIT:
                    break
        edges.append({'id': f'vehicle_comention_{a}_{b}', 'source_entity_id': a, 'target_entity_id': b,
                      'relationship_type': 'co_mentioned_in_documents', 'weight': count,
                      'date_first': None, 'date_last': None,
                      'metadata': {'meaning': 'Vehicle name with nearby vehicle vocabulary and person/place name occur in the same documents (near-duplicate copies counted once). A reading lead, NOT evidence of ownership, travel, meeting or wrongdoing.',
                                   'shared_documents': count, 'specificity_score': round(weighted[a, b], 3),
                                   'lift_vs_chance': round(count * total_documents / (counts[a] * counts[b]), 2),
                                   'documents_mentioning_source': counts[a], 'documents_mentioning_target': counts[b],
                                   'new_link': frozenset((a, b)) not in linked,
                                   'sample_documents': [{'efta_number': d} for d in sample],
                                   'search_phrases': {'source': sorted(phrases[a]), 'target': sorted(phrases[b])},
                                   'complete_evidence': 'output/vehicle_evidence.json; target names use scripts/build_comentions.py phrase rules'}})
    return edges


def write_json(path, value):
    partial = path.with_suffix('.partial.json')
    partial.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    partial.replace(path)


def main():
    started = time.monotonic()
    out = ROOT / 'output'
    specs = load_review(ROOT / 'config/vehicles.json')
    entities = json.loads((out / 'normalized_entities.json').read_text())
    relationships = json.loads((out / 'normalized_relationships.json').read_text())
    by_id = {e['id']: e for e in entities}
    for name in ('place_nodes.json', 'email_place_nodes.json'):
        if name == 'email_place_nodes.json' and not (ROOT / 'data/private/email_places.db').is_file():
            continue
        if (out / name).exists():
            for entity in json.loads((out / name).read_text()):
                by_id.setdefault(entity['id'], entity)
    excluded = survivor_ids(list(by_id.values()), relationships, config=ROOT / 'config/survivors.json')
    targets = [e for e in by_id.values() if e['entity_type'] in TARGET_TYPES
               and e['id'] not in excluded and not e.get('suspected_parse_artifact')]
    registry = json.loads((ROOT / 'data/raw/persons_registry.json').read_text())
    aliases = registry_aliases(entities, registry)
    canonical = canonical_documents(out / 'document_clusters.db')
    with closing(corpus.connect(corpus.PATH)) as db:
        candidates, pages = discover(db, canonical)
        print(f'Discovery: {pages:,} FTS pages, {len(candidates):,} candidates (review required)', flush=True)
        docs, samples = reviewed_mentions(db, specs, canonical)
    nodes, vehicle_docs, vehicle_phrases, rejected = make_nodes(specs, docs, samples, entities, canonical)
    page_doc, doc_names = page_map(path=corpus.PATH, cache=out / 'corpus_page_map.npz')
    total_documents = distinct_count(doc_names, canonical)
    target_docs, target_phrases = target_mentions(targets, aliases, page_doc, doc_names, corpus.PATH)
    del page_doc, doc_names
    edges = make_edges(vehicle_docs, target_docs, {**vehicle_phrases, **target_phrases},
                       canonical, total_documents, relationships)
    wanted = {d['efta_number'] for e in edges for d in e['metadata']['sample_documents']}
    wanted.update(d for n in nodes for d in n['metadata'].get('sample_eftas', []))
    urls = corpus.document_urls(wanted)
    for edge in edges:
        for d in edge['metadata']['sample_documents']:
            if d['efta_number'] in urls:
                d['pdf_url'] = urls[d['efta_number']]
    evidence = [{'entity_id': n['id'], 'name': n['name'], 'documents': sorted(vehicle_docs[n['id']]),
                 'search_phrases': sorted(vehicle_phrases[n['id']])}
                for n in nodes]
    for i, spec in enumerate(specs):
        candidates.append({'name': spec['name'], 'kind': spec['kind'], 'reviewed': True,
                           'document_count': len(docs[i]), 'distinct_documents': distinct_count(docs[i], canonical),
                           'sample_eftas': sorted(samples[i], key=sample_key),
                           'samples': [{'efta_number': d, 'snippet': samples[i][d]}
                                       for d in sorted(samples[i], key=sample_key)]})
    summary = {'vehicle_nodes': len(nodes), 'vehicle_types': dict(Counter(n['vehicle_type'] for n in nodes)),
               'reused_kg_ids': sorted(n['id'] for n in nodes if isinstance(n['id'], int)),
               'edges': len(edges), 'min_shared_documents': MIN_DOCUMENTS,
               'discovery_pages': pages, 'candidates': len(candidates), 'rejected_reviews': rejected,
               'survivors_excluded': len(excluded),
               'vehicles': [{'name': n['name'], 'id': n['id'], 'document_count': n['metadata']['document_count'],
                             'distinct_documents': n['metadata']['distinct_documents'],
                             'sample_eftas': n['metadata'].get('sample_eftas', []),
                             'corpus_corroborated': n['metadata']['corpus_corroborated']} for n in nodes],
               'seconds': round(time.monotonic() - started, 1),
               'warning': 'Regex candidates require manual review. Existing KG aircraft are retained even without corpus support. Co-mention never establishes ownership or use; registration reuse is explained in config/vehicles.json.'}
    from contact_publication import text_scrubber
    candidates = text_scrubber({})(candidates)
    for filename, value in [('vehicle_candidates.json', candidates), ('vehicle_nodes.json', nodes),
                            ('vehicle_edges.json', edges), ('vehicle_evidence.json', evidence),
                            ('vehicle_summary.json', summary)]:
        write_json(out / filename, value)
    report = [f'{len(nodes)} vehicle nodes; {len(edges)} vehicle-to-person/place co-mention edges.',
              '', 'Counts require nearby vehicle vocabulary; near-duplicate copies count once.', '',
              '| Vehicle | Kind | Documents | Distinct documents | Sample evidence |',
              '| --- | --- | ---: | ---: | --- |']
    for node in nodes:
        m = node['metadata']
        citations = ', '.join(f'[{d}]({urls[d]})' if d in urls else d for d in m.get('sample_eftas', [])[:2])
        report.append(f'| {node["name"]} | {node["vehicle_type"]} | {m["document_count"]} | {m["distinct_documents"]} | {citations or "Uncorroborated existing KG node"} |')
    report += ['', 'KG IDs 493–496 are preserved. N34JE is retained without corroboration or corpus edges.',
               '', 'N212JE was reused for two Gulfstream airframes; its original KG Cessna label conflicts with the corpus. Registration history and alias decisions are documented in config/vehicles.json.',
               '', 'Some network boats were discussed for charter or purchase only; inclusion does not establish Epstein ownership or completed use. Co-mention is a reading lead, not evidence of travel, ownership, a meeting or wrongdoing.',
               '', 'Reviewed snippets: output/vehicle_candidates.json (reviewed=true); full vehicle document lists: output/vehicle_evidence.json.', '']
    (out / 'vehicle_report.md').write_text('\n'.join(report))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
