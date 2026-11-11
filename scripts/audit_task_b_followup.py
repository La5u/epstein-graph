"""Predict build-rule effects on the private viewer snapshot, without a rebuild.

Reads bounded layer files and SQLite aggregates; never loads combined graphs.
Labels, samples and evidence are written only under data/private/audit/.
"""
from collections import Counter, defaultdict
import json
from pathlib import Path
import random
import re
import sqlite3

from airports import Airports
from bounded_json import items
from build_companies import visibility_reason
from entity_build_evidence import LabelEvidence
from entity_build_resolution import BuildIndex, wrong_type, website_org_links, viewer_references
from entity_build_rules import text_reason, website_reason, website_ocr_target
from entity_labels import dictionary_words
from normalize_graph import is_parse_artifact

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'data/private/audit/task_b_followup3'


def private_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def main():
    DEST.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot = json.loads((ROOT/'data/private/audit/viewer_nodes.json').read_text())
    findings = json.loads((ROOT/'data/private/audit/full_read/all_findings.json').read_text())
    labels = {str(n['id']): (n['real'] or n['label']).strip() for n in snapshot}
    kindmap = {'place': 'location', 'contact': 'email'}
    nodes = {str(n['id']): {**n['rec'], 'id': str(n['id']), 'name': labels[str(n['id'])],
                           'entity_type': kindmap.get(n['type'], n['type'])} for n in snapshot}
    source = {}
    for filename in ('normalized_entities.json', 'company_nodes.json', 'place_nodes.json', 'email_place_nodes.json'):
        for node in items(ROOT/'output'/filename):
            identity = str(node['id'])
            if identity in nodes:
                source[identity] = node
                for field in ('aliases', 'original_names', 'icao', 'airport_ident'):
                    if field in node:
                        nodes[identity][field] = node[field]
    evidence = LabelEvidence.project()
    evidence.prepare_companies(n['name'] for n in nodes.values() if n['entity_type'] in {'organization', 'shell_company'})
    dictionary = dictionary_words()
    hosts = [n['name'].casefold() for n in nodes.values() if n['entity_type'] == 'website']
    evidence.prepare_domains(hosts)
    gaz = json.loads((ROOT/'config/entity_gazetteer.json').read_text())
    references = {str(n['id']): {**n, 'id': str(n['id'])} for n in viewer_references(ROOT/'output', labels.values(), gaz).values()}
    evidence.prepare_reference_brands(references)
    index = BuildIndex({**references, **nodes}, gaz, evidence=evidence, airports=Airports())
    target_labels = {i: n['name'] for i, n in index.existing.items()}
    print('Reference evidence and index loaded', flush=True)
    with sqlite3.connect(f'file:{ROOT / "output/entity_graph.db"}?mode=ro', uri=True) as db:
        db.executescript('PRAGMA cache_size=-16000; PRAGMA temp_store=FILE;')
        counts = dict(db.execute('SELECT name,document_count FROM entities WHERE entity_type="website"'))
    print('Website document counts loaded from built entity table', flush=True)
    buckets = defaultdict(dict)
    for domain, count in counts.items():
        base, _, suffix = domain.rpartition('.')
        buckets[len(base), suffix][domain] = count
    predictions = {}
    after = {i: dict(n) for i, n in nodes.items()}
    for identity, node in nodes.items():
        value, kind, result = node['name'], node['entity_type'], None
        if kind == 'survivor':
            continue
        if kind in {'person', 'participant', 'organization', 'shell_company'}:
            target, corrected, fields, rule = index.cross_type(value, kind, {identity})
            if target is not None:
                result = ['fold', rule, target]
            elif corrected:
                result = ['retype', rule, corrected]
                after[identity].update(entity_type=corrected, **fields)
                if corrected == 'website':
                    from entity_build_resolution import website_domain
                    domain = website_domain(value)
                    if reason := website_reason(domain, (value,), dictionary, domain in evidence.domains):
                        result = ['hide', reason]
        if result is None and kind in {'organization', 'shell_company'}:
            clean, target, rule = index.company_repair(value, {identity})
            detected, fields = wrong_type(clean)
            if target is not None:
                result = ['fold', rule, target]
            elif detected:
                result = ['retype', 'machine_format', detected]
                after[identity].update(entity_type=detected, **fields)
            elif reason := index.company_reason(clean):
                result = ['hide', reason]
            elif clean != value:
                result = ['clean', 'company_prefix', clean]
                after[identity]['name'] = clean
            elif node.get('label_quality') == 'not_a_name':
                meta = source.get(identity, {}).get('metadata') or {}
                meta = json.loads(meta) if isinstance(meta, str) else meta
                model_kind = meta.get('company_kind')
                if model_kind in {'generic', 'junk'} and visibility_reason(clean, model_kind, evidence) is None:
                    result = ['restore', 'attested_or_surname_firm']
                    after[identity].pop('label_quality', None)
        if result is None and kind in {'organization', 'shell_company', 'participant', 'person', 'location'}:
            if reason := text_reason(value):
                result = ['hide', reason]
        if result is None and kind == 'person' and is_parse_artifact(value):
            result = ['hide', 'flight_artifact']
        if result is None and kind in {'person', 'participant'}:
            detected, fields = wrong_type(value)
            if detected and detected != 'email':
                result = ['retype', 'machine_format', detected]
                after[identity].update(entity_type=detected, **fields)
        if result is None and kind == 'website':
            base, _, suffix = value.rpartition('.')
            canonical = website_ocr_target(value, buckets[len(base), suffix])
            if canonical != value:
                result = ['fold', 'ocr_website_variant', canonical]
            elif reason := website_reason(value.casefold(), (value,), dictionary, value in evidence.domains):
                result = ['hide', reason]
        if result:
            predictions[identity] = result
            if result[0] == 'hide':
                after[identity]['label_quality'] = 'not_a_name'
    flagged, per_class = set(), defaultdict(set)
    themes = defaultdict(set)
    for finding in findings:
        good = {str(i) for i in finding['good']} & nodes.keys()
        flagged |= good
        per_class[finding['class']] |= good
        c = finding['class'].casefold()
        fix = finding['fix'].casefold()
        orgs = {i for i in good if nodes[i]['entity_type'] in {'organization', 'shell_company'}}
        if re.search(r'clause|generic|stub|boilerplate|ner_junk', c):
            themes['boilerplate_stub_organizations'] |= orgs
        if re.search(r'prefix|leading|citation|noise', c):
            themes['organization_prefix_noise'] |= orgs
        if re.search(r'trunc|ocr', c):
            themes['organization_truncation_ocr'] |= orgs
        if re.search(r'wrong|as_person|as_participant|participant_org|org_as', c):
            themes['wrong_types'] |= good
        if re.search(r'org|company|brand|airline|institute', c + ' ' + fix):
            themes['organizations_as_people'] |= {i for i in good if nodes[i]['entity_type'] in {'person', 'participant'}}
        if re.search(r'place|airport|city|location|geograph', c + ' ' + fix):
            themes['places_as_people_or_organizations'] |= {i for i in good if nodes[i]['entity_type'] in {'person', 'participant', 'organization'}}
        if re.search(r'junk|fragment|boilerplate|metadata|header|calendar|markup|garbage|file', c):
            themes['entity_junk_all_types'] |= good
        themes['websites'] |= {i for i in good if nodes[i]['entity_type'] == 'website'}
        if re.search(r'duplicat|dup|variant|legal_form', c):
            themes['organization_duplicates'] |= orgs
    touched = set(predictions)
    def recall(ids):
        kept = {i for i in ids & touched if predictions[i][0] == 'restore'}
        return {'flagged': len(ids), 'fixed': len(ids & touched - kept), 'untouched': len(ids - touched),
                'retained_despite_flag': len(kept),
                'actions': dict(Counter(predictions[i][0] for i in ids & touched))}
    rng = random.Random(2026110706)
    samples = {}
    for title, ids in [('unflagged', touched - flagged), ('flagged', touched & flagged)]:
        chosen = rng.sample(sorted(ids), min(40, len(ids)))
        samples[title] = [{'id': i, 'label': labels[i], 'type': nodes[i]['entity_type'], 'action': predictions[i],
                           'target_label': target_labels.get(str(predictions[i][2])) if len(predictions[i]) > 2 else None,
                           'findings': [f for f in findings if i in set(map(str, f['good']))]} for i in chosen]
    baseline_path = Path.home()/'.cache/epstein-sol/c/pred.json'
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    associations = website_org_links(after.values())
    report = {'snapshot_nodes': len(nodes), 'flagged_nodes': len(flagged), 'touched': len(touched),
              'actions': dict(Counter(p[0] for p in predictions.values())),
              'rules': dict(Counter(p[1] for p in predictions.values())),
              'unflagged_touched': len(touched - flagged), 'themes': {k: recall(v) for k, v in themes.items()},
              'unflagged_by_rule': dict(Counter(predictions[i][1] for i in touched - flagged)),
              'per_class': {k: recall(v) for k, v in per_class.items()},
              'website_of_snapshot_links': len(associations), 'website_of_baseline_links': len(website_org_links(nodes.values())),
              'website_of_flagged_sites': len({str(e['source_entity_id']) for e in associations} & flagged),
              'website_of_unflagged_sites': len({str(e['source_entity_id']) for e in associations} - flagged),
              'attested_weak_domains': sorted(evidence.domains),
              'baseline_touched': len(baseline), 'newly_touched': len(touched - baseline.keys()),
              'no_longer_touched': len(baseline.keys() - touched),
              'survivors_touched': sum(nodes[i]['entity_type'] == 'survivor' for i in predictions)}
    private_json(DEST/'predictions.json', predictions)
    private_json(DEST/'report.json', report)
    private_json(DEST/'samples.json', samples)
    private_json(DEST/'website_of.json', associations)
    print(json.dumps({k: v for k, v in report.items() if k not in {'per_class', 'attested_weak_domains'}}, indent=2))


if __name__ == '__main__':
    main()
