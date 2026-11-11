"""Document index: every PDF and data record behind each graph link, and per node.

    .venv/bin/python scripts/build_documents.py

Sources, per link type (endpoints are the combined graph's canonical IDs):
  email headers        every message in communication_graph.db evidence for the
                       pair and type (EFTA documents, or the Jmail email page)
  co-mentions          documents naming both (comention_graph.db mentions)
  survivor designators designator documents (victim_documents.json) that also
                       name the entity
  places in emails     every email behind the link (email_place_documents.json)
  flights              Epstein Exposed flight record pages (the handwritten logs'
                       OCR is too damaged to match flights to pages: ~2%)
  everything else      EFTA numbers cited in the record; if none, pages whose
                       text names both endpoints (full-text fallback), and the
                       curated place's Epstein Exposed page for reviewed place links
  node pages           Epstein Exposed place and person profile pages (exact name)
A node's documents are the union of its links' documents, plus a designator's
own documents; a node with none gets the pages naming it. In each list a copy or
subset of another document (dedupe_documents.py) is dropped when the document
that contains it (its cluster's canonical) is in the same list. Writes output/documents.db:
  edge_documents(edge_id, efta, page, how)   page is 0-based or NULL
  edge_records(edge_id, kind, ref, url)      non-EFTA data (flight, jmail email, place page)
  node_documents(node_id, efta, edge_id, page)  edge_id NULL for the node's own documents;
                                             page (0-based) only on those, when known
  node_records(node_id, kind, ref, url)
and output/documents_summary.json. Pseudonymous endpoints are searched by
their real values from data/private/, but only EFTA numbers are written.
"""
import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import corpus
from corpus_index import canonical_documents
from document_ranges import page_locator
from derived_files import replace_if_changed

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'
PRIVATE = ROOT / 'data/private'
HEADER_TYPES = ('sent_to', 'cc_to', 'header_label_association', 'upstream_resolved_header_identity')
FALLBACK_LIMIT = 200  # full-text fallback documents per link
JMAIL = 'https://jmail.world/thread/'
FLIGHT = 'https://epsteinexposed.com/flights/'
LOCATION = 'https://epsteinexposed.com/locations/'
PERSON = 'https://epsteinexposed.com/people/'


def comm_id(node):
    return f'kg:{node}' if isinstance(node, int) else node


def header_evidence(path, wanted):
    """{(source, target, type): ([(efta, None)], [jmail doc ids])} for the wanted triples."""
    found = defaultdict(lambda: (set(), set()))
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        for source, target, kind, efta, table in db.execute(
                'SELECT canonical_source, canonical_target, relationship_type, efta_number, source_table FROM evidence'):
            key = (source, target, kind)
            if key not in wanted:
                continue
            if efta:
                found[key][0].add(efta)
            elif table.startswith('jmail:'):
                found[key][1].add(table[6:])
    return found


def comention_docs(path, ids):
    """{entity id: set(efta)} for the requested KG entities."""
    docs = defaultdict(set)
    if not Path(path).exists() or not ids:
        return docs
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        ordered = sorted(ids, key=str)
        for offset in range(0, len(ordered), 500):
            chunk = ordered[offset:offset + 500]
            marks = ','.join('?' * len(chunk))
            for entity, efta in db.execute(f'SELECT entity_id, efta_number FROM mentions WHERE entity_id IN ({marks})', chunk):
                docs[entity].add(efta)
    return docs


def search_names(entity, private):
    """Phrases that name a node in the text: label, original names, real values."""
    names = [private.get(entity['id'])] if entity['id'] in private else []
    if not str(entity['id']).startswith(('email_', 'phone_', 'participant_', 'address_')):
        names += [entity['name'], *(entity.get('original_names') or [])]
    out = []
    for name in names:
        tokens = re.findall(r'\w+', name or '')
        if tokens and sum(len(t) for t in tokens) >= 3 and '?' not in (name or ''):
            out.append(' '.join(tokens))
    return list(dict.fromkeys(out))[:6]


def named(db, names, limit=FALLBACK_LIMIT):
    """{efta: page} of the first pages naming any of names."""
    if not names:
        return {}
    query = ' OR '.join(f'"{n}"' for n in names)
    found = {}
    for efta, page in corpus.search_pages(db, query, limit * 4):
        if len(found) >= limit:
            break
        found.setdefault(efta, page)
    return found


def place_pages(entities):
    """{node id: Epstein Exposed location URL} for curated places in the graph."""
    path = ROOT / 'data/external/epsteinexposed/locations.json'
    if not path.exists():
        return {}
    slugs = {loc['slug'] for loc in json.loads(path.read_text())['data']}
    by_name = {loc['name'].casefold(): loc['slug'] for loc in json.loads(path.read_text())['data']}
    pages = {}
    for e in entities:
        slug = str(e['id'])[6:] if str(e['id']).startswith('place_') else None
        slug = slug if slug in slugs else by_name.get(e['name'].casefold())
        if slug and e['entity_type'] in ('location', 'property'):
            pages[e['id']] = LOCATION + slug
    return pages


def person_pages(entities):
    """{node id: Epstein Exposed profile URL} for people whose label or original name is
    exactly one profile's name or alias (no fuzzy matching)."""
    path = ROOT / 'data/external/epsteinexposed/persons.json'
    if not path.exists():
        return {}
    from normalize_graph import name_key
    slugs = defaultdict(set)
    for person in json.loads(path.read_text())['data']:
        for label in [person['name'], *(person.get('aliases') or [])]:
            slugs[name_key(label)].add(person['slug'])
    pages = {}
    for e in entities:
        if e['entity_type'] != 'person' or e.get('suspected_parse_artifact'):
            continue
        found = {slug for label in [e['name'], *(e.get('original_names') or [])] for slug in slugs.get(name_key(label), ())}
        if len(found) == 1 and ' ' in e['name'].strip():
            pages[e['id']] = PERSON + found.pop()
    return pages


def both_named(db, a_names, b_names, limit=FALLBACK_LIMIT):
    """[(efta, page)] of pages whose text holds a name of each endpoint."""
    if not a_names or not b_names:
        return []
    side = lambda names: '(' + ' OR '.join(f'"{n}"' for n in names) + ')'
    query = f'{side(a_names)} AND {side(b_names)}'
    rows = corpus.search_pages(db, query, limit * 4)
    seen, out = set(), []
    for efta, page in rows:
        if efta not in seen and len(seen) < limit:
            seen.add(efta)
            out.append((efta, page))
    return out


def load_private():
    """{node id: real value} for pseudonymous nodes (private lookups, if present)."""
    values = {}
    for name in ('contact_lookup.json', 'address_lookup.json'):
        path = PRIVATE / name
        if path.exists():
            values.update({k: v['value'] for k, v in json.loads(path.read_text()).items()})
    return values


def load_json(name, default):
    path = OUT / name
    return json.loads(path.read_text()) if path.exists() else default


def build(entities, relationships, db=None, private=None):
    """-> (edge_docs {edge: {efta: (page, how)}}, edge_records {edge: [(kind, ref, url)]},
    own {node: {efta: page}}, node_records {node: [(kind, ref, url)]}, links found by the fallback)."""
    private = private or {}
    by_id = {e['id']: e for e in entities}
    edge_docs, records = defaultdict(dict), defaultdict(list)
    locate = page_locator(db)

    def add(edge, efta, how, page=None):
        if efta not in edge_docs[edge] or edge_docs[edge][efta][0] is None:
            edge_docs[edge][efta] = (page, how)

    wanted = {(comm_id(r['source_entity_id']), comm_id(r['target_entity_id']), r['relationship_type']): r['id']
              for r in relationships if r['relationship_type'] in HEADER_TYPES}
    if wanted and (OUT / 'communication_graph.db').exists():
        for key, (eftas, jmail) in header_evidence(OUT / 'communication_graph.db', wanted).items():
            for efta in eftas:
                add(wanted[key], efta, 'email header')
            records[wanted[key]] += [('jmail email', doc, JMAIL + doc) for doc in sorted(jmail)]

    kg_ids = {x for r in relationships if r['relationship_type'] in ('co_mentioned_in_documents', 'victim_document_co_mention')
              for x in (r['source_entity_id'], r['target_entity_id']) if isinstance(x, int)}
    mentions = comention_docs(OUT / 'comention_graph.db', kg_ids)
    company_ids = {x for r in relationships if r['relationship_type'] == 'co_mentioned_in_documents'
                   for x in (r['source_entity_id'], r['target_entity_id']) if str(x).startswith('company_')}
    mentions.update(comention_docs(OUT / 'company_graph.db', company_ids))  # build_companies.py
    entity_ids = {x for r in relationships if str(r['id']).startswith('entity_comention_')
                  for x in (r['source_entity_id'], r['target_entity_id'])}
    entity_docs = comention_docs(OUT / 'entity_graph.db', entity_ids)
    for node, docs in entity_docs.items():
        mentions[node].update(docs)
    victim_docs = {k: set(v) for k, v in load_json('victim_documents.json', {}).items()}
    place_docs = load_json('email_place_documents.json', {})
    flights = load_json('flight_index.json', {})
    flights_of, on_date = defaultdict(set), defaultdict(set)  # graph id / date -> flight ids
    for flight_id, flight in flights.items():
        on_date[flight['date']].add(flight_id)
        for x in flight['people'] + flight['places']:
            flights_of[x].add(flight_id)
    # Upstream flight-log pairs whose passengers the export does not name: their dates.
    pair_dates = defaultdict(set)
    for r in relationships:
        if r['relationship_type'] == 'traveled_with':
            m = r['metadata'] if isinstance(r['metadata'], dict) else json.loads(r['metadata'] or '{}')
            pair_dates[frozenset((r['source_entity_id'], r['target_entity_id']))].update(m.get('sample_dates') or [])

    def add_flight(edge, flight_id, how):
        f = flights[flight_id]
        records[edge].append(('flight', f"{f['date']} {f['origin']} → {f['destination']}" + how, FLIGHT + flight_id))

    for r in relationships:
        edge, kind = r['id'], r['relationship_type']
        a, b = r['source_entity_id'], r['target_entity_id']
        for efta in corpus.cited_documents(r.get('metadata')):
            document, page = locate(efta)
            add(edge, document, 'cited in the record', page or None)
        if kind == 'co_mentioned_in_documents':
            for efta in mentions.get(a, set()) & mentions.get(b, set()):
                add(edge, efta, 'names both')
        elif kind == 'victim_document_co_mention':
            designator, other = (a, b) if a in victim_docs else (b, a)
            for efta in victim_docs.get(designator, set()) & mentions.get(other, set()):
                add(edge, efta, 'designator and name in one document')
        elif kind == 'mentioned_place_in_email':
            for efta in place_docs.get(edge, []):
                add(edge, efta, 'email mentions the place')
        if kind in ('traveled_with', 'flew_to_or_from', 'associated_with'):
            shared = flights_of[a] & flights_of[b] if kind != 'associated_with' else set()
            for flight_id in sorted(shared):
                add_flight(edge, flight_id, '')
            if not shared and not edge_docs.get(edge):
                # Same-day flights carrying either person, else any flight that day.
                for date in sorted(pair_dates.get(frozenset((a, b)), ())):
                    either = on_date[date] & (flights_of[a] | flights_of[b])
                    for flight_id in sorted(either or on_date[date]):
                        add_flight(edge, flight_id, ' (same date' + ('' if either else '; passengers not matched') + ')')

    places = place_pages(entities)
    node_records = defaultdict(list)
    for node, url in places.items():
        node_records[node].append(('place page', by_id[node]['name'], url))
    for node, url in person_pages(entities).items():
        node_records[node].append(('profile page', by_id[node]['name'], url))
    for r in relationships:
        if not edge_docs.get(r['id']) and not records.get(r['id']):
            for node in (r['source_entity_id'], r['target_entity_id']):
                if node in places:
                    records[r['id']].append(('place page', by_id[node]['name'], places[node]))
    fallback = 0
    if db is not None:
        for r in relationships:
            if edge_docs.get(r['id']):
                continue
            a, b = by_id.get(r['source_entity_id']), by_id.get(r['target_entity_id'])
            if a is None or b is None or a is b:
                continue
            hits = both_named(db, search_names(a, private), search_names(b, private))
            fallback += bool(hits)
            for efta, page in hits:
                add(r['id'], efta, 'full text names both (fallback)', page)
    own = defaultdict(dict)
    for node, docs in victim_docs.items():
        own[node].update(dict.fromkeys(docs))
    if db is not None:
        covered = {x for r in relationships if edge_docs.get(r['id']) for x in (r['source_entity_id'], r['target_entity_id'])}
        # Full-text entity layer nodes get their documents straight from its mentions (see write());
        # a name search for each of its ~900k nodes would take many hours.
        entity_graph = OUT / 'entity_graph.db'
        if entity_graph.exists():
            with closing(sqlite3.connect(f'file:{entity_graph}?mode=ro', uri=True)) as evidence:
                covered.update(i for (i,) in evidence.execute('SELECT DISTINCT entity_id FROM mentions'))
        for e in entities:
            if e['id'] not in covered and e['id'] not in own:
                own[e['id']].update(named(db, search_names(e, private)))
    return edge_docs, records, own, node_records, fallback


def keep_supersets(lists, canonical_of):
    """Drop, in place, each document whose canonical (the copy or superset kept by
    dedupe_documents.py) is in the same list; lists: {key: {efta: value}}. -> number dropped."""
    dropped = 0
    for docs in lists.values():
        covered = [efta for efta in docs if canonical_of.get(efta, efta) != efta and canonical_of[efta] in docs]
        for efta in covered:
            del docs[efta]
        dropped += len(covered)
    return dropped


def write(path, relationships, edge_docs, records, own, node_records, entity_graph=None):
    """Built beside the target and swapped in, so readers never see a half-written index."""
    final, path = path, path.with_name('.' + path.name + '.tmp')
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript('''CREATE TABLE edge_documents(edge_id TEXT, efta TEXT, page INTEGER, how TEXT);
                            CREATE TABLE edge_records(edge_id TEXT, kind TEXT, ref TEXT, url TEXT);
                            CREATE TABLE node_documents(node_id TEXT, efta TEXT, edge_id TEXT, page INTEGER);
                            CREATE TABLE node_records(node_id TEXT, kind TEXT, ref TEXT, url TEXT);''')
        db.executemany('INSERT INTO node_records VALUES (?,?,?,?)',
                       ((str(n), *item) for n, items in node_records.items() for item in items))
        db.executemany('INSERT INTO edge_documents VALUES (?,?,?,?)',
                       ((str(e), efta, page, how) for e, docs in edge_docs.items() for efta, (page, how) in sorted(docs.items())))
        db.executemany('INSERT INTO edge_records VALUES (?,?,?,?)',
                       ((str(e), *item) for e, items in records.items() for item in items))
        rows = set()
        for r in relationships:
            for efta in edge_docs.get(r['id'], {}):
                for node in (r['source_entity_id'], r['target_entity_id']):
                    rows.add((str(node), efta, str(r['id']), None))
        rows |= {(str(node), efta, None, page) for node, docs in own.items() for efta, page in docs.items()}
        db.executemany('INSERT INTO node_documents VALUES (?,?,?,?)', sorted(rows, key=lambda x: (x[0], x[1], x[2] or '')))
        if entity_graph and Path(entity_graph).exists():
            db.execute('ATTACH DATABASE ? AS entity_evidence', (str(entity_graph),))
            db.execute("INSERT INTO node_documents SELECT cast(entity_id AS TEXT),efta_number,NULL,NULL FROM entity_evidence.mentions")
        db.executescript('''CREATE INDEX edge_documents_edge ON edge_documents(edge_id);
                            CREATE INDEX edge_records_edge ON edge_records(edge_id);
                            CREATE INDEX node_documents_node ON node_documents(node_id);''')
    replace_if_changed(path, final)


def main():
    entities = json.loads((OUT / 'graph_entities.json').read_text())
    relationships = json.loads((OUT / 'graph_relationships.json').read_text())
    db = corpus.connect() if corpus.PATH.exists() else None
    edge_docs, records, own, node_records, fallback = build(entities, relationships, db, load_private())
    if db is not None:
        db.close()
    canonical_of = canonical_documents()
    hidden = keep_supersets(edge_docs, canonical_of) + keep_supersets(own, canonical_of)
    write(OUT / 'documents.db', relationships, edge_docs, records, own, node_records, OUT / 'entity_graph.db')
    linked = {r['id'] for r in relationships if edge_docs.get(r['id']) or records.get(r['id'])}
    node_linked = ({x for r in relationships if r['id'] in linked for x in (r['source_entity_id'], r['target_entity_id'])}
                   | {n for n, docs in own.items() if docs} | set(node_records))
    entity_graph = OUT / 'entity_graph.db'
    if entity_graph.exists():
        with closing(sqlite3.connect(f'file:{entity_graph}?mode=ro', uri=True)) as evidence:
            node_linked.update(i for (i,) in evidence.execute('SELECT id FROM entities'))
    by_type = defaultdict(Counter)
    for r in relationships:
        by_type[r['relationship_type']]['linked' if r['id'] in linked else 'none'] += 1
    nodes_by_type = defaultdict(Counter)
    for e in entities:
        nodes_by_type[e['entity_type']]['linked' if e['id'] in node_linked else 'none'] += 1
    summary = {'links': len(relationships), 'links_with_documents': len(linked),
               'links_from_full_text_fallback': fallback,
               'copies_dropped_for_their_superset': hidden,
               'nodes': len(entities), 'nodes_with_documents': len(node_linked & {e['id'] for e in entities}),
               'links_by_type': {k: dict(v) for k, v in sorted(by_type.items())},
               'nodes_by_type': {k: dict(v) for k, v in sorted(nodes_by_type.items())},
               'note': 'A document behind a link is a reading lead. Full-text fallback pages name both endpoints; '
                       'they do not show a relationship.'}
    (OUT / 'documents_summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
