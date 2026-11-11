#!/usr/bin/env python3
"""Offline name-candidate audit, not identity resolution or graph ingestion."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import unicodedata

from audit_epsteinexposed_api import atomic, save_json, validate_rows
from survivor_policy import metadata, survivor_ids

ROOT = Path(__file__).resolve().parents[1]
KINDS = {'person': 'persons', 'organization': 'organizations', 'shell_company': 'organizations'}
STATUSES = ('exact_candidate', 'ambiguous', 'unmatched', 'excluded')
NOTICE = ('Name candidates only, not authoritative identity assertions. Primary-name link counts '
          'are lexical coverage, not verified identities. No identity, legal status, or graph updates. '
          'Only existing local snapshots were read; no API requests were made.')


def name_key(value):
    """Canonical Unicode casefold; punctuation becomes whitespace, never deleted."""
    text = unicodedata.normalize('NFC', value).casefold()
    text = ''.join(' ' if unicodedata.category(c).startswith('P') else c for c in text)
    return ' '.join(unicodedata.normalize('NFC', text).split())


def aliases(row):
    value = row.get('aliases')
    if isinstance(value, str):
        # Graph aliases may be a JSON-encoded list; never split arbitrary prose.
        try:
            value = json.loads(value)
        except ValueError:
            value = []
    return [v for v in (value or []) if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def names(row):
    originals = row.get('original_names') or []
    return [row.get('name', ''), *aliases(row),
            *[n for n in originals if isinstance(n, str)]]


def keys(row, kind):
    # Organization divisions and legal suffixes are retained; no alias expansion.
    values = names(row) if kind == 'persons' else [row['name']]
    return {name_key(n) for n in values if n and name_key(n)}


def exclusions(entities, relationships, exports, config):
    configured = json.loads(Path(config).read_text())['names']
    if not isinstance(configured, list) or any(not isinstance(n, str) for n in configured):
        raise ValueError('invalid exclusions')
    excluded = survivor_ids(entities, relationships, config=config)
    blocked = {name_key(n) for n in configured if name_key(n)}
    # Shared policy plus explicit upstream victim labels; never infer from biography.
    for row in exports['persons']:
        if str(row.get('category', '')).casefold() in ('victim', 'survivor') or str(metadata(row).get('person_type', '')).casefold() == 'victim':
            blocked.update(keys(row, 'persons'))
    for row in entities:
        if row['id'] in excluded or row.get('entity_type') in ('victim', 'survivor'):
            excluded.add(row['id'])
            blocked.update(keys(row, 'persons'))
    # Propagate excluded aliases conservatively, including across export names.
    changed = True
    while changed:
        before = len(blocked)
        for row in [*entities, *exports['persons']]:
            row_keys = keys(row, 'persons')
            if row_keys & blocked:
                blocked.update(row_keys)
        changed = len(blocked) != before
    excluded.update(row['id'] for row in entities if keys(row, 'persons') & blocked)
    return excluded, blocked


def reconcile(entities, relationships, exports, config):
    excluded, blocked = exclusions(entities, relationships, exports, config)
    public = {'schema_version': 1, 'notice': NOTICE, 'datasets': {},
              'normalized_entities_total': len(entities),
              'normalized_incompatible_kind_count': sum(e.get('entity_type') not in KINDS for e in entities)}
    private = {'schema_version': 1, 'notice': NOTICE,
               'review_status': 'unreviewed_candidates_require_manual_review', 'datasets': {}}
    for kind in ('persons', 'organizations'):
        local = [e for e in entities if KINDS.get(e.get('entity_type')) == kind]
        index = defaultdict(set)
        by_id = {e['id']: e for e in local}
        for row in local:
            if row['id'] not in excluded:
                for key in keys(row, kind):
                    index[key].add(row['id'])
        records, counts, reverse = [], Counter(), defaultdict(set)
        for row in exports[kind]:
            if keys(row, kind) & blocked:
                counts['excluded'] += 1
                continue  # No excluded identities or aliases in candidate output.
            hits = set().union(*(index[k] for k in keys(row, kind)))
            record = {'upstream_id': row['id'], 'upstream_name': row['name'],
                      'upstream_slug': row.get('slug'), 'candidates': []}
            for eid in sorted(hits, key=str):
                entity = by_id[eid]
                primary = name_key(row['name']) == name_key(entity['name'])
                record['candidates'].append({'normalized_id': eid, 'normalized_name': entity['name'],
                                             'basis': 'primary_name' if primary else 'exact_alias'})
                reverse[eid].add(row['id'])
            records.append(record)
        for record in records:
            candidates = record['candidates']
            record['status'] = ('unmatched' if not candidates else
                                'ambiguous' if len(candidates) > 1 or any(
                                    len(reverse[c['normalized_id']]) > 1 for c in candidates)
                                else 'exact_candidate')
            counts[record['status']] += 1
        eligible = [e for e in local if e['id'] not in excluded]
        links = [c for r in records for c in r['candidates']]
        public['datasets'][kind] = {
            'export_total': len(exports[kind]),
            'export_status_counts': {s: counts[s] for s in STATUSES},
            'normalized_total': len(local), 'normalized_excluded': len(local) - len(eligible),
            'normalized_with_candidates': len(reverse),
            'normalized_without_candidates': sum(e['id'] not in reverse for e in eligible),
            'candidate_links': len(links),
            'primary_name_links': sum(c['basis'] == 'primary_name' for c in links),
            'alias_links': sum(c['basis'] == 'exact_alias' for c in links)}
        private['datasets'][kind] = {
            'export_candidates': records,
            'normalized_without_candidates': [{'normalized_id': e['id'], 'normalized_name': e['name']}
                                               for e in eligible if e['id'] not in reverse]}
    return public, private


def load_inputs(root):
    entities = json.loads((root / 'output/normalized_entities.json').read_text())
    relationships = json.loads((root / 'output/normalized_relationships.json').read_text())
    if not isinstance(entities, list) or not isinstance(relationships, list):
        raise ValueError('invalid normalized schema')
    ids = set()
    for row in entities:
        if row['id'] in ids or not isinstance(row.get('name'), str) or not row['name'].strip():
            raise ValueError('invalid normalized entity')
        ids.add(row['id'])
    exports = {}
    for kind in ('persons', 'organizations'):
        value = json.loads((root / 'data/external/epsteinexposed' / (kind + '.json')).read_text())
        exports[kind] = validate_rows(value['data'])
        if any(not isinstance(r.get('name'), str) or not r['name'].strip() for r in exports[kind]):
            raise ValueError('invalid export name')
    return entities, relationships, exports


def run(root=ROOT):
    root = Path(root)
    public, private = reconcile(*load_inputs(root), root / 'config/survivors.json')
    directory = root / 'data/private'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Atomic private writes never leave an intermediate world-readable file.
    save_json(directory / 'epsteinexposed_api_entity_candidates.json', private)
    output = root / 'output'
    output.mkdir(exist_ok=True)
    save_json(output / 'api_entity_coverage.json', public, private=False)
    lines = ['# API entity name-candidate coverage', '', NOTICE, '',
             'Exact Unicode casefold/whitespace and punctuation-to-whitespace matching only. '
             'Legal suffixes and bank divisions are retained. Person aliases require unique '
             'candidates on both sides; collisions remain ambiguous. Exclusions include configured '
             'names and normalized excluded aliases. Excluded identities are omitted from private candidates.', '',
             '| Dataset | Export total | Exact candidate | Ambiguous | Unmatched | Excluded | Primary links | Alias links |',
             '|---|---:|---:|---:|---:|---:|---:|---:|']
    for kind, row in public['datasets'].items():
        counts = row['export_status_counts']
        lines.append('| ' + kind + ' | ' + ' | '.join(str(n) for n in
                     [row['export_total'], *[counts[s] for s in STATUSES],
                      row['primary_name_links'], row['alias_links']]) + ' |')
        lines.append('')
    lines += ['Normalized-side gaps and totals are recorded in the aggregate JSON. '
              'Coverage is limited to these snapshots; unmatched names are not evidence of missing identities.']
    atomic(output / 'api_entity_coverage.md', ('\n'.join(lines) + '\n').encode(), private=False)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        return run(args.root)
    except (OSError, ValueError, TypeError, KeyError):
        print('Reconciliation failed: local input, exclusions, or report error (details suppressed).')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
