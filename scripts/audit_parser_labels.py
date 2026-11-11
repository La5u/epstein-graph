"""Private, bounded-memory audit of parser rules against a viewer snapshot.

Read small upstream node layers and SQLite entity metadata, never graph.json or
graph_entities.json. All label-bearing reports go under data/private/audit.
This is a prediction from the current snapshot, not a graph rebuild.
"""
from collections import Counter, defaultdict
from contextlib import closing
import csv
import json
import os
from pathlib import Path
import re
import sqlite3

from build_companies import normal_name
from entity_build_rules import (clean_label, clean_company_name, company_label_reason,
                                company_tail, text_reason, website_reason, website_ocr_target)
from entity_labels import dictionary_words
from normalize_graph import is_parse_artifact

ROOT = Path(__file__).resolve().parents[1]


def audit(root=ROOT):
    snapshot = json.loads((root/'data/private/audit/viewer_nodes.json').read_text())
    wanted = {n['id'] for n in snapshot}
    # Exported edges precede viewer_documents.augment's fallback links.
    with (root/'output/graph_relationships.json').open() as stream:
        exported = {i for edge in json.load(stream) for i in (edge['source_entity_id'], edge['target_entity_id'])}
    records, sources = {}, defaultdict(set)
    for filename in ('normalized_entities.json', 'communication_nodes.json', 'place_nodes.json',
                     'email_place_nodes.json', 'company_nodes.json', 'contact_nodes.json',
                     'victim_nodes.json', 'vehicle_nodes.json'):
        path = root/'output'/filename
        if path.exists():
            for node in json.loads(path.read_text()):
                if node['id'] in wanted:
                    records[node['id']] = node
                    sources[node['id']].add(filename.removesuffix('_nodes.json').removesuffix('.json'))
    website_counts = {}
    with closing(sqlite3.connect(f'file:{root}/output/entity_graph.db?mode=ro', uri=True)) as db:
        db.execute('PRAGMA cache_size=-16000')
        for identity, payload in db.execute('SELECT id,metadata FROM entities'):
            if identity in wanted:
                records[identity] = json.loads(payload)
                sources[identity].add('entity')
        website_counts = dict(db.execute("SELECT name,document_count FROM entities WHERE entity_type='website'"))
    dictionary = dictionary_words()
    company_names = defaultdict(set)
    for identity, node in records.items():
        if node['entity_type'] in ('organization', 'shell_company'):
            if not company_label_reason(node['name']):
                company_names[normal_name(node['name'])].add(identity)
    # Inspect raw website spellings, retaining explicit URL/email evidence.
    web_spellings = defaultdict(list)
    domains = {n['real'] or n['label'] for n in snapshot if n['type'] == 'website'}
    mentions = root / 'data/private/entity_mentions.db'
    if not mentions.exists(): mentions = root / 'output/.build/entity_mentions.db'
    with closing(sqlite3.connect(f'file:{mentions}?mode=ro', uri=True)) as db:
        db.execute('PRAGMA cache_size=-16000')
        db.execute('CREATE TEMP TABLE wanted(key PRIMARY KEY)')
        db.executemany('INSERT INTO wanted VALUES (?)', ((d,) for d in domains))
        for domain, spelling in db.execute('SELECT DISTINCT m.key,m.spelling FROM wanted w JOIN mentions m ON m.key=w.key'):
            web_spellings[domain].append(spelling)
    counts = Counter()
    long_classes = Counter()
    rows, long_rows = [], []
    for node in snapshot:
        name = node['real'] or node['label']
        clean = clean_label(name)
        kind, identity = node['type'], node['id']
        source = '+'.join(sorted(sources[identity])) or 'snapshot_only'
        previous = records.get(identity, node.get('rec', {}))
        already_hidden = previous.get('label_quality') == 'not_a_name' or bool(previous.get('suspected_parse_artifact'))
        reason, target, action = None, None, 'retain'
        if kind == 'website':
            reason = website_reason(name, web_spellings.get(name, ()), dictionary)
            target = website_ocr_target(name, website_counts)
            if target == name:
                target = None
        elif kind == 'person' and is_parse_artifact(name):
            reason = 'flight_log_parse_artifact'
        elif kind == 'organization' and (str(identity).startswith('company_') or source.startswith('company')):
            clean = clean_company_name(name)
            exact = company_names.get(normal_name(clean), set()) - {identity} if clean != name else set()
            tail, hits = company_tail(clean, lambda t: company_names.get(normal_name(t), ()))
            if len(exact) == 1:
                target = next(iter(exact))
            elif len(hits) == 1 and identity not in hits:
                target = next(iter(hits))
            reason = company_label_reason(tail if target else clean)
        else:
            reason = text_reason(clean)
        if reason:
            action = 'hide'
            counts['hide:' + reason] += 1
            counts['previously_hidden_matches' if already_hidden else 'new_hidden_matches'] += 1
        elif target:
            action = 'fold_existing'
            counts['fold:' + kind] += 1
        elif clean != name:
            action = 'clean_label'
            counts['clean:' + kind] += 1
        if node['deg'] == 0 and str(identity).startswith('company_'):
            counts['zero_degree_company_nodes'] += 1
        if str(identity).startswith('company_') and identity not in exported:
            counts['company_nodes_without_exported_link'] += 1
        address = kind == 'place' and (node.get('sub') == 'address' or bool(re.match(r'^\d+\s', clean)))
        row = {'id': identity, 'label': name, 'type': kind, 'subtype': node.get('sub'), 'source': source,
               'words': len(name.split()), 'degree': node['deg'], 'address': address,
               'action': action, 'reason': reason, 'cleaned_label': clean, 'fold_target': target,
               'already_hidden': already_hidden, 'exported_link': identity in exported}
        if row['words'] > 5:
            category = ('address_signature' if address and clean != name else 'address' if address else
                        reason if reason else 'prefixed_company' if target else
                        'signature_annotation' if clean != name and kind == 'place' else
                        'cleaned_company_leadin' if clean != name and kind == 'organization' else
                        'retained_long_organization_candidate' if kind == 'organization' else
                        'long_place_name' if kind == 'place' else
                        'delegated_header_label' if re.match(r'^(?:on|n) behalf of\b', name, re.I) else
                        'header_name_with_affiliation')
            row['class'] = category
            long_classes[kind, source, category] += 1
            long_rows.append(row)
        if action != 'retain':
            rows.append(row)
    long_rows.sort(key=lambda r: (r['type'], r['source'], r['class'], r['label']))
    report = {'snapshot_nodes': len(snapshot), 'rules': dict(counts),
              'long_labels': len(long_rows), 'long_non_address_labels': sum(not n['address'] for n in long_rows),
              'long_classes': [{'type': kind, 'source': source, 'class': category, 'count': count}
                               for (kind, source, category), count in sorted(long_classes.items())],
              'note': 'Predicted rule effects on the snapshot; a full rebuild was not run.',
              'changes': rows, 'all_long_labels': long_rows}
    destination = root/'data/private/audit/task_b_parser_labels.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    for csv_path, selected in ((destination.with_suffix('.csv'), long_rows),
                               (destination.with_suffix('.non_address.csv'), [r for r in long_rows if not r['address']])):
        descriptor = os.open(csv_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(long_rows[0]) if long_rows else ['id', 'label', 'type', 'source', 'class'])
            writer.writeheader()
            writer.writerows(selected)
    print(json.dumps({k: v for k, v in report.items() if k not in ('changes', 'all_long_labels')}, indent=2))
    return report


if __name__ == '__main__':
    audit()
