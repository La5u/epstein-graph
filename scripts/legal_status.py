"""Evidence-bearing status markers, never inferred from graph connections.

Upstream status labels are historical/unverified, not current custody findings.
Missing labels and 'no_action' must never be treated as cleared/exonerated.
"""
import datetime
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
STATUS_LABELS = {'convicted': 'reported conviction', 'in_custody': 'reported custody',
                 'under_investigation': 'reported investigation', 'deceased': 'reported deceased'}
UPSTREAM_MAP = {'convicted': 'convicted', 'in_custody': 'in_custody',
                'incarcerated': 'in_custody', 'in_prison': 'in_custody',
                'under_investigation': 'under_investigation', 'deceased': 'deceased'}


def annotate(entities, annotations, source_url):
    by_id = {e['id']: e for e in entities}
    for entity in entities:
        markers = []
        if entity['entity_type'] == 'person':
            for record in entity.get('original_records', [entity]):
                metadata = record.get('metadata') or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except ValueError:
                        continue
                if not isinstance(metadata, dict):
                    continue
                status = UPSTREAM_MAP.get(metadata.get('legal_status'))
                if status:
                    markers.append({'status': status, 'source': source_url,
                                    'source_entity_id': record['id'], 'source_field': 'metadata.legal_status',
                                    'as_of': None, 'current_status_verified': False,
                                    'note': 'Upstream label only; status date unspecified. Not a current custody/investigation check.'})
        entity['legal_status_evidence'] = markers
    for item in annotations:
        entity = by_id.get(item['entity_id'])
        if entity is None or entity['entity_type'] != 'person':
            raise ValueError('Status annotations require an existing canonical person ID')
        if item['status'] not in STATUS_LABELS:
            raise ValueError('Unsupported legal status marker')
        url = urlparse(item['source_url'])
        if url.scheme != 'https' or not url.netloc:
            raise ValueError('Status annotation requires an HTTPS evidence URL')
        datetime.date.fromisoformat(item['as_of'])
        entity['legal_status_evidence'].append({'status': item['status'], 'source': item['source_url'],
                                               'as_of': item['as_of'], 'current_status_verified': False,
                                               'note': item.get('note', 'Dated source assertion; not a live status check.')})
    counts = Counter()
    for entity in entities:
        flags = sorted({e['status'] for e in entity['legal_status_evidence']})
        entity['legal_status_flags'] = flags
        counts.update(flags)
    return {status: counts[status] for status in STATUS_LABELS}


def load_annotations(entities):
    path = ROOT / 'config/legal_status_annotations.json'
    annotations = json.loads(path.read_text())['annotations'] if path.exists() else []
    commit_path = ROOT / 'data/raw/upstream-commit.txt'
    commit = commit_path.read_text().strip() if commit_path.exists() else 'main'
    url = f'https://github.com/rhowardstone/Epstein-research-data/blob/{commit}/knowledge_graph_entities.json'
    counts = annotate(entities, annotations, url)
    (ROOT / 'output/legal_status_summary.json').write_text(json.dumps({
        'marker_counts': counts,
        'warning': 'Incomplete upstream-reported/dated annotations, NOT verified current legal status. No marker means unknown, not cleared. Custody, conviction, investigation and death are separate.'
    }, indent=2) + '\n')
    return counts
