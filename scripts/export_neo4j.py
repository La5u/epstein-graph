"""Export the graph for Neo4j (and Gephi / anything that reads GraphML).

    .venv/bin/python scripts/export_neo4j.py            # viewer graph, pseudonymous -> output/neo4j/
    .venv/bin/python scripts/export_neo4j.py --full     # + every communication identity and pair edge, every
                                                        #   full-text company and contact value (+ their pairs)
    .venv/bin/python scripts/export_neo4j.py --private  # real names/addresses -> data/private/neo4j/ (0600)

Writes, per run:
  nodes.csv          neo4j-admin header format: id:ID, name, entity_type, :LABEL (Entity;Person ...) + properties
  relationships.csv  :START_ID, :END_ID, :TYPE (SENT_TO ...), weight, date_first, date_last, relationship_type
  aliases.csv        merged identities (address / spelling) as Alias nodes ...
  alias_of.csv       ... with ALIAS_OF -> canonical (rule, confidence): every merge stays auditable
  import.cypher      LOAD CSV script for a running database (files in its import/ directory)
  graph.graphml      the same nodes and relationships for Gephi, yEd, networkx, igraph
  README.txt         the neo4j-admin command line

IDs are the pipeline's own (kg:<n> for graph entities, email_/participant_/phone_ pseudonyms, place_...),
so re-exports line up and merges can be replayed. Multi-valued properties use ';' (neo4j-admin
array delimiter). Pseudonymous exports contain no real contact values; --private reads them from
data/private/contact_lookup.json and the email-place lookup and writes only under data/private/.
"""
import argparse
import csv
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import networkx as nx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'
PRIVATE = ROOT / 'data/private'
LABELS = {'person': 'Person', 'survivor': 'Survivor', 'organization': 'Organization', 'shell_company': 'ShellCompany',
          'location': 'Location', 'property': 'Property', 'aircraft': 'Aircraft', 'email': 'EmailAddress',
          'phone': 'Phone', 'participant': 'Participant', 'website': 'Website'}
# Scalar properties copied when present (others stay in the JSON graph files).
PROPERTIES = ('relevance', 'subtype', 'place_type', 'address', 'municipality', 'icao', 'iata', 'lat', 'lng', 'designator', 'label_quality',
              'suspected_parse_artifact', 'document_count', 'service_line', 'in_headers', 'likely_owner', 'email_document_count', 'full_metadata_unique_connections', 'source')
ARRAYS = ('legal_status_flags', 'original_names', 'aliases')


def node_id(value):
    """Graph entity IDs are ints in the JSON files; everywhere else they are kg:<n>."""
    return f'kg:{value}' if isinstance(value, int) or (isinstance(value, str) and value.isdigit()) else str(value)


def labels_for(entity):
    kind = entity.get('entity_type', '')
    labels = ['Entity', LABELS.get(kind, re.sub(r'(?:^|_)(\w)', lambda m: m.group(1).upper(), kind) or 'Unknown')]
    if entity.get('place_type') == 'airport':
        labels.append('Airport')
    if entity.get('suspected_parse_artifact'):
        labels.append('ParseArtifact')
    return labels


def array(value):
    """neo4j-admin array cell: list (or JSON list string) joined with ';'."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.startswith('[') else [value]
        except ValueError:
            value = [value]
    return ';'.join(str(v).replace(';', ',') for v in value or [] if v not in (None, ''))


def real_names():
    """{pseudonym ID: real value} (private exports only)."""
    names = {}
    for name in ('contact_lookup.json', 'address_lookup.json', 'fulltext_contact_lookup.json'):
        path = PRIVATE / name
        if path.exists():
            names.update({k: v['value'] for k, v in json.loads(path.read_text()).items()})
    return names


def view_graph():
    entities = json.loads((OUT / 'graph_entities.json').read_text())
    relationships = json.loads((OUT / 'graph_relationships.json').read_text())
    nodes = {node_id(e['id']): e for e in entities}
    edges = [(node_id(r['source_entity_id']), node_id(r['target_entity_id']), r['relationship_type'], r.get('weight') or 1,
              r.get('date_first'), r.get('date_last'), str(r['id'])) for r in relationships]
    return nodes, edges


def full_communication_graph(nodes, edges):
    """Add every identity and directed pair edge of output/communication_graph.db (canonical IDs)."""
    db_path = OUT / 'communication_graph.db'
    with closing(sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)) as db:
        canonical = dict(db.execute('SELECT member_id, canonical_id FROM identity_map'))
        for identity, kind, label in db.execute('SELECT id, entity_type, label FROM nodes'):
            if identity not in canonical and identity not in nodes:
                nodes.setdefault(identity, {'id': identity, 'name': label, 'entity_type': kind})
        seen = {(a, b, t) for a, b, t, *_ in edges}
        for a, b, kind, count, first, last in db.execute(
                "SELECT source_id, target_id, relationship_type, record_count, date_first, date_last FROM edges WHERE relationship_type IN ('sent_to', 'cc_to')"):
            if a != b and (a, b, kind) not in seen:
                edges.append((a, b, kind, count, first, last, f'comm:{a}:{b}:{kind}'))
    return nodes, edges


def full_text_layers(nodes, edges):
    """Add every full-text company (output/company_graph.db) and every email address / phone
    number written in the corpus (output/contact_graph.db) with all their pairs; returns
    [(member, canonical, rule, confidence)] aliases for contact values that are OCR variants
    or merged members of another node."""
    aliases = []
    seen = {(a, b, t) for a, b, t, *_ in edges}
    def add_edge(a, b, kind, weight, edge):
        if a != b and (a, b, kind) not in seen and (b, a, kind) not in seen:
            seen.add((a, b, kind))
            edges.append((a, b, kind, weight, None, None, edge))
    company_db = OUT / 'company_graph.db'
    if company_db.exists():
        with closing(sqlite3.connect(f'file:{company_db}?mode=ro', uri=True)) as db:
            for identity, name, kind, documents in db.execute('SELECT id, name, entity_type, document_count FROM entities'):
                nodes.setdefault(identity, {'id': identity, 'name': name, 'entity_type': kind, 'document_count': documents,
                                            'source': 'full-text legal name (build_companies.py)'})
            for a, b, shared in db.execute('SELECT a, b, shared_documents FROM pairs'):
                add_edge(node_id(a), node_id(b), 'co_mentioned_in_documents', shared, f'company:{a}:{b}')
    entity_db = OUT / 'entity_graph.db'
    if entity_db.exists():
        with closing(sqlite3.connect(f'file:{entity_db}?mode=ro', uri=True)) as db:
            for identity, payload in db.execute('SELECT id, metadata FROM entities'):
                nodes.setdefault(node_id(identity), json.loads(payload))
            for a, b, shared in db.execute('SELECT a,b,shared_documents FROM pairs'):
                add_edge(node_id(a), node_id(b), 'co_mentioned_in_documents', shared, f'entity:{a}:{b}')
    contact_db = OUT / 'contact_graph.db'
    if contact_db.exists():
        with closing(sqlite3.connect(f'file:{contact_db}?mode=ro', uri=True)) as db:
            columns = {r[1] for r in db.execute('PRAGMA table_info(nodes)')}
            owner = 'owner' if 'owner' in columns else 'NULL'
            for identity, kind, canonical, in_headers, service, documents, labels, likely_owner in db.execute(
                    f'SELECT id, kind, canonical, in_headers, service_line, documents, labels, {owner} FROM nodes'):
                canonical = node_id(canonical)
                if canonical != identity:
                    aliases.append((identity, canonical, 'fulltext_ocr_variant' if not in_headers else 'identity_map', 'probable'))
                elif identity not in nodes:
                    quality = json.loads(labels).get('label_quality') if kind == 'participant' and labels else None
                    nodes[identity] = {'id': identity, 'name': identity, 'entity_type': kind, 'document_count': documents or None,
                                       'service_line': service, 'in_headers': in_headers, 'label_quality': quality,
                                       'likely_owner': node_id(likely_owner) if likely_owner else None,
                                       'source': 'name written next to a contact value (extract_contacts.py)' if kind == 'participant'
                                       else 'written in the full text (extract_contacts.py); pseudonymous value'}
            for a, b, shared in db.execute('SELECT a, b, shared_documents FROM pairs'):
                add_edge(node_id(a), node_id(b), 'written_on_same_page', shared, f'contact:{a}:{b}')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'names'").fetchone():
                for value, person, documents in db.execute('SELECT value, identity, documents FROM names'):
                    add_edge(node_id(value), node_id(person), 'written_with_name', documents, f'name:{value}:{person}')
    return aliases


def write_csv(path, header, rows, private):
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    if private:
        path.chmod(0o600)


def export(full=False, private=False, out_dir=None):
    out_dir = Path(out_dir or (PRIVATE / 'neo4j' if private else OUT / 'neo4j'))
    out_dir.mkdir(parents=True, exist_ok=True)
    if private:
        out_dir.chmod(0o700)
    nodes, edges = view_graph()
    extra_aliases = []
    if full:
        nodes, edges = full_communication_graph(nodes, edges)
        extra_aliases = full_text_layers(nodes, edges)
    names = real_names() if private else {}
    name_of = lambda i, e: names.get(i, e.get('name', i))
    edges = [e for e in edges if e[0] in nodes and e[1] in nodes]

    header = ['id:ID', 'name', 'entity_type', ':LABEL', *PROPERTIES, *(f'{k}:string[]' for k in ARRAYS)]
    write_csv(out_dir / 'nodes.csv', header, (
        [i, name_of(i, e), e.get('entity_type', ''), ';'.join(labels_for(e)),
         *('' if e.get(k) is None else e[k] for k in PROPERTIES), *(array(e.get(k)) for k in ARRAYS)]
        for i, e in nodes.items()), private)
    write_csv(out_dir / 'relationships.csv', [':START_ID', ':END_ID', ':TYPE', 'weight:float', 'date_first', 'date_last', 'relationship_type', 'edge_id'],
              ([a, b, kind.upper(), weight, first or '', last or '', kind, edge] for a, b, kind, weight, first, last, edge in edges), private)
    identity_map = json.loads((OUT / 'identity_map.json').read_text()) if (OUT / 'identity_map.json').exists() else {}
    aliases = [(member, node_id(item['canonical']), item['rule'], item['confidence']) for member, item in identity_map.items()
               if node_id(item['canonical']) in nodes]
    known = {m for m, *_ in aliases}
    aliases += [a for a in extra_aliases if a[0] not in known and a[1] in nodes and a[0] not in nodes]
    write_csv(out_dir / 'aliases.csv', ['id:ID', 'name', ':LABEL'],
              ([m, names.get(m, m), 'Alias'] for m, *_ in aliases), private)
    write_csv(out_dir / 'alias_of.csv', [':START_ID', ':END_ID', ':TYPE', 'rule', 'confidence'],
              ([m, c, 'ALIAS_OF', rule, confidence] for m, c, rule, confidence in aliases), private)

    (out_dir / 'import.cypher').write_text('''// Run with the four CSVs in the database's import/ directory (neo4j 5).
CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT alias_id IF NOT EXISTS FOR (n:Alias) REQUIRE n.id IS UNIQUE;
LOAD CSV WITH HEADERS FROM 'file:///nodes.csv' AS row
CALL { WITH row
  MERGE (n:Entity {id: row.`id:ID`})
  SET n.name = row.name, n.entity_type = row.entity_type, n.place_type = row.place_type, n.source = row.source,
      n.legal_status_flags = CASE row.`legal_status_flags:string[]` WHEN '' THEN [] ELSE split(row.`legal_status_flags:string[]`, ';') END,
      n.original_names = CASE row.`original_names:string[]` WHEN '' THEN [] ELSE split(row.`original_names:string[]`, ';') END
  WITH n, row CALL apoc.create.addLabels(n, split(row.`:LABEL`, ';')) YIELD node RETURN count(*) AS c
} IN TRANSACTIONS OF 5000 ROWS;
// Without APOC, drop the apoc line above and set labels per type, e.g.:
// MATCH (n:Entity {entity_type: 'person'}) SET n:Person;
LOAD CSV WITH HEADERS FROM 'file:///relationships.csv' AS row
CALL { WITH row
  MATCH (a:Entity {id: row.`:START_ID`}), (b:Entity {id: row.`:END_ID`})
  CALL apoc.create.relationship(a, row.`:TYPE`, {weight: toFloat(row.`weight:float`), date_first: row.date_first,
       date_last: row.date_last, edge_id: row.edge_id}, b) YIELD rel RETURN count(*) AS c
} IN TRANSACTIONS OF 5000 ROWS;
LOAD CSV WITH HEADERS FROM 'file:///aliases.csv' AS row
CALL { WITH row MERGE (:Alias {id: row.`id:ID`, name: row.name}) } IN TRANSACTIONS OF 5000 ROWS;
LOAD CSV WITH HEADERS FROM 'file:///alias_of.csv' AS row
CALL { WITH row
  MATCH (a:Alias {id: row.`:START_ID`}), (c:Entity {id: row.`:END_ID`})
  MERGE (a)-[:ALIAS_OF {rule: row.rule, confidence: row.confidence}]->(c)
} IN TRANSACTIONS OF 5000 ROWS;
''')
    (out_dir / 'README.txt').write_text(f'''Offline bulk import (empty database, neo4j 5):
  neo4j-admin database import full --array-delimiter=';' --overwrite-destination \\
    --nodes={out_dir}/nodes.csv --nodes={out_dir}/aliases.csv \\
    --relationships={out_dir}/relationships.csv --relationships={out_dir}/alias_of.csv neo4j
Running database: copy the CSVs to its import/ directory and run import.cypher (uses APOC; see the comment there).
GraphML (Gephi, yEd, networkx.read_graphml): graph.graphml.
{"PRIVATE: real names and contact details. Keep local; do not share." if private else "Pseudonymous: contact values are HMAC IDs; real names only in a --private export."}
''')

    graph = nx.MultiDiGraph()
    for i, e in nodes.items():
        graph.add_node(i, name=str(name_of(i, e)), entity_type=e.get('entity_type', ''), labels=';'.join(labels_for(e)),
                       **{k: e[k] for k in ('place_type', 'lat', 'lng') if e.get(k) is not None})
    for a, b, kind, weight, first, last, edge in edges:
        graph.add_edge(a, b, key=edge, relationship_type=kind, weight=float(weight), date_first=first or '', date_last=last or '')
    nx.write_graphml(graph, out_dir / 'graph.graphml')
    if private:
        for path in out_dir.iterdir():
            path.chmod(0o600)
    return {'directory': str(out_dir), 'nodes': len(nodes), 'relationships': len(edges), 'aliases': len(aliases)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--full', action='store_true', help='also every communication identity and pair edge')
    parser.add_argument('--private', action='store_true', help='real names; written under data/private/neo4j')
    parser.add_argument('--out', help='output directory (default output/neo4j or data/private/neo4j)')
    args = parser.parse_args()
    if args.private and args.out and not Path(args.out).resolve().is_relative_to(PRIVATE.resolve()):
        parser.error('--private exports must stay under data/private/')
    print(json.dumps(export(args.full, args.private, args.out), indent=2))


if __name__ == '__main__':
    main()
