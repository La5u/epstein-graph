"""Separate 'survivor' entity type for curated survivor/victim-labelled nodes.

Names come only from curated structured lists: config/survivors.json,
config/victims.json, and persons_registry.json entries whose category is
"victim", plus upstream entities with person_type "victim" and victim_of
sources. No corpus search, OCR or redaction recovery is used to identify
anyone. Missing or malformed configs degrade to an empty set, never widen it.
"""
import hashlib
import hmac
import json
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SURVIVOR_CONFIG = ROOT / 'config/survivors.json'
VICTIM_CONFIG = ROOT / 'config/victims.json'
REGISTRY = ROOT / 'data/raw/persons_registry.json'


def name_key(name):
    return ' '.join(unicodedata.normalize('NFC', str(name)).casefold().split())


def metadata(entity):
    value = entity.get('metadata')
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _records(entity):
    """The entity plus the upstream records merged into it (normalize_graph.py)."""
    return [entity, *(r for r in entity.get('original_records') or [] if isinstance(r, dict))]


def labelled_victim(entity):
    """Upstream person_type 'victim' on the entity or any record merged into it,
    so an exact-name merge that kept an unlabelled record never drops the label."""
    return any(str(metadata(r).get('person_type', '')).casefold() == 'victim' for r in _records(entity))


def entity_names(entity):
    """Display name, original names and merged-record names, for list matching."""
    names = [entity.get('name', ''), *entity.get('original_names', [])]
    names += [r.get('name', '') for r in _records(entity)[1:]]
    return [n for n in names if n]


def victim_of_sources(relationships):
    return {r['source_entity_id'] for r in relationships
            if r.get('relationship_type') == 'victim_of' and 'source_entity_id' in r}


def _config_names(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    values = data.get('names', []) if isinstance(data, dict) else []
    return [v for v in values if isinstance(v, str) and v.strip()]


def registry_victim_names(path=None):
    path = Path(path) if path is not None else REGISTRY
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    names = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get('category', '')).casefold() == 'victim':
            names.append(row.get('name', ''))
            names.extend(row.get('aliases') or [])
    return names


def victim_names(config=None, survivor_config=None, registry=None):
    names = _config_names(Path(config) if config is not None else VICTIM_CONFIG)
    names += _config_names(Path(survivor_config) if survivor_config is not None else SURVIVOR_CONFIG)
    names += registry_victim_names(registry)
    return {name_key(n) for n in names if n and name_key(n)}


def participant_ids(names, key=None):
    """HeaderLabel participant IDs that exactly these names would get in
    build_communications (same normalization and keyed hash), so a listed
    survivor who appears only as an email-header name is matched too."""
    if key is None:
        from build_contacts import contact_key  # local: build_contacts imports this module
        key = contact_key()
    return {'participant_' + hmac.new(key, ('header_label\0' + name).encode(), hashlib.sha256).hexdigest()[:24]
            for name in names}


def victim_ids(entities, relationships=(), config=None, survivor_config=None, registry=None, key=None):
    """Ids of entities that should render as the 'survivor' type."""
    keys = victim_names(config=config, survivor_config=survivor_config, registry=registry)
    participants = participant_ids(keys, key)
    ids = {e['id'] for e in entities
           if labelled_victim(e) or e['id'] in participants or any(name_key(n) in keys for n in entity_names(e))}
    return ids | victim_of_sources(relationships)


def dual_status_ids(entities, survivor_config=None, key=None):
    """Ids of entities named in survivors.json 'dual_status': also named as co-conspirators,
    publicly self-described victims. They keep their type; the viewer marks them half red."""
    try:
        data = json.loads(Path(survivor_config if survivor_config is not None else SURVIVOR_CONFIG).read_text())
        names = data.get('dual_status', {}).get('names', [])
    except (OSError, ValueError, AttributeError):
        return set()
    keys = {name_key(n) for n in names if isinstance(n, str) and n.strip()}
    if not keys:
        return set()
    participants = participant_ids(keys, key)
    return {e['id'] for e in entities if e['id'] in participants or any(name_key(n) in keys for n in entity_names(e))}


def extra_nodes(config=None):
    """Config-defined victims with no upstream graph node, as standalone nodes."""
    path = Path(config) if config is not None else VICTIM_CONFIG
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    nodes = data.get('extra_nodes', []) if isinstance(data, dict) else []
    return [dict(node) for node in nodes if isinstance(node, dict) and node.get('name')]
