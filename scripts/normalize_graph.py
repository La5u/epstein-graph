"""Conservative name normalization; raw inputs remain untouched.

Exact normalized name/type equality is a candidate identity rule, not general
entity resolution. No fuzzy, surname, alias or middle-name merging is attempted.
Every merge is recorded for review and original node/edge records are retained.
"""
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from entity_build_rules import clean_label, text_reason, company_label_reason, has_company_form

ROOT = Path(__file__).resolve().parents[1]


def name_key(name):
    return ' '.join(unicodedata.normalize('NFC', name).casefold().split())


AIRPORT_ROUTE = re.compile(r'^[A-Z0-9]{3,5} [^,]+, ([A-Z]{2}, United States|St\. Thomas,?|Bahamas|.*United)$')
ARTIFACT = re.compile(r'\bFOIA\b|\bNo Records\b|^Passengers?\s*(?:$|[,:(]|\d+\b|\b(?:No|None|Test|Unknown|Empty)\b)|\bNo No\b', re.I)
FLIGHT_RECORD = re.compile(r'^\d+-[A-Z]-[\w-]*\bN\d{2,5}[A-Z]{0,2}(?:-|$)', re.I)


ROLE_SUFFIX = re.compile(r'\s*\((pilot|chef|co-?pilot|nanny|driver|butler|housekeeper|chauffeur|cook|steward|stewardess|crew)\)\s*$', re.I)


def clean_display_name(name):
    """Repair flight-log "Last, First" labels: "Mmifomobel, Nadia Nadia" ->
    "Ntado Mmifomobel", "Koluk-Koylu, Banu Banu Koluk-" -> "Bfol Kbali-Kmice"."""
    name = ROLE_SUFFIX.sub('', ' '.join(name.replace('\xa0', ' ').split()))  # role kept in original_names
    if name.count(',') != 1 or is_parse_artifact(name):
        return name
    last, rest = (part.strip() for part in name.split(','))
    given = []
    for token in rest.split():
        if token in given or (token.endswith('-') and last.startswith(token)):
            continue  # repeated given name or truncated copy of the surname
        given.append(token)
    if not last or not given or not re.fullmatch(r"[^\W\d_][\w'.-]*", last) or not all(re.fullmatch(r"[^\W\d_][\w'.-]*", t) for t in given):
        return name
    return ' '.join(given + [last])


def is_parse_artifact(name):
    """Flight-log route strings and FOIA record IDs mis-typed as people upstream."""
    name = ' '.join(name.replace('\xa0', ' ').split())
    return bool(AIRPORT_ROUTE.match(name) or ARTIFACT.search(name) or FLIGHT_RECORD.match(name) or name.startswith('?,')
                or re.match(r'^(?:[A-Z]{3,4}\s*[-/]\s*[A-Z]{3,4})(?:\b|$)', name)
                or re.match(r'^(?:PBI|TIST|TISX|TEB|JFK|LGA|FLL|MIA|EWR|LAX|STT)\s+[A-Z][a-z]', name))


def registry_aliases(entities, registry):
    """Map KG person id -> extra aliases, only when the registry match is unambiguous."""
    persons = defaultdict(list)
    for entity in entities:
        if entity['entity_type'] == 'person':
            persons[entity['normalized_name']].append(entity['id'])
    owners = defaultdict(set)
    for index, row in enumerate(registry):
        for label in [row['name']] + (row.get('aliases') or []):
            owners[name_key(label)].add(index)
    row_people = defaultdict(set)
    for key, ids in persons.items():
        for index in owners.get(key, ()):
            row_people[index].update(ids)
    aliases = defaultdict(list)
    for key, ids in persons.items():
        rows = owners.get(key, set())
        # Both directions must be one-to-one: one KG person, one registry row.
        if len(ids) != 1 or len(rows) != 1 or len(row_people[next(iter(rows))]) != 1:
            continue
        row = registry[next(iter(rows))]
        for alias in [row['name']] + (row.get('aliases') or []):
            # The alias must not name a different KG person or registry entry.
            alias_key = name_key(alias)
            if alias_key != key and persons.get(alias_key, ids) == ids and owners[alias_key] == rows:
                aliases[ids[0]].append(alias)
    return aliases


def load_identity_merges():
    """Reviewed merges from config/identity_merges.json: member -> (canonical, record)."""
    path = ROOT / 'config/identity_merges.json'
    if not path.exists():
        return {}
    mapping = {}
    for item in json.loads(path.read_text()).get('merges', []):
        for member in item['members']:
            if member in mapping and mapping[member][0] != item['canonical']:
                raise ValueError(f'{member} is merged into two different identities')
            mapping[member] = (item['canonical'], item)
    return mapping


def final_target(curated, member):
    """End of a reviewed merge chain (A->B, B->C gives C); cycles are config errors."""
    seen = {member}
    target = curated[member][0]
    while isinstance(target, int) and target in curated:
        if target in seen:
            raise ValueError(f'reviewed merges form a cycle through {target}')
        seen.add(target)
        target = curated[target][0]
    return target


def person_review():
    path = ROOT / 'config/node_review.json'
    return json.loads(path.read_text()).get('persons', {}) if path.exists() else {}


def normalize(entities, relationships, curated=None, evidence=None, airports=None):
    curated = load_identity_merges() if curated is None else curated
    review = person_review()
    # Audit verdict "variant_of_known" merges a person into the named graph person.
    ids_by_name = {name_key(e['name']): e['id'] for e in entities if e['entity_type'] == 'person'}
    for key, item in review.items():
        target = ids_by_name.get(name_key(item.get('canonical_name', '')))
        if item['verdict'] == 'variant_of_known' and target is not None and int(key) != target and int(key) not in curated:
            curated = {**curated, int(key): (target, {'confidence': 'reviewed', 'reason': 'node audit: variant of ' + item['canonical_name']})}
    # A flight-log first name with "?" for the surname, resolved from a document ("Frances ?" -> "Frances Jrehabe").
    # Without a surname the "?" stays (fix_label "Nadia ?" -> "Nadia" is not applied).
    labels = {int(k): v['canonical_name'] for k, v in review.items()
              if v.get('verdict') == 'fix_label' and len(v.get('canonical_name', '').split()) >= 2 and k.isdigit()}
    entities = [{**e, 'name': labels[e['id']]} if e['entity_type'] == 'person' and e['id'] in labels and '?' in e['name'] else e
                for e in entities]
    originals = {e['id']: e for e in entities}
    from entity_build_resolution import BuildIndex, wrong_type, website_domain
    from entity_build_rules import clean_company_name
    from survivor_policy import survivor_ids
    protected = survivor_ids(entities, relationships)
    if evidence:
        evidence.prepare_companies(e['name'] for e in entities if e['entity_type'] in {'organization', 'shell_company'} and e['id'] not in protected)
    index = BuildIndex(originals, evidence=evidence, airports=airports)
    prepared = []
    for entity in entities:
        node = dict(entity)
        identity = node['id']
        if identity not in protected:
            display = node['name']
            if node['entity_type'] in ('organization', 'shell_company'):
                display, target, rule = index.company_repair(display, exclude={identity})
                node['name'] = display
                kind, fields = wrong_type(display)
                place_target, place_kind, place_fields, place_rule = index.cross_type(display, node['entity_type'], exclude={identity})
                if place_kind == 'location':
                    kind, fields, target, rule = place_kind, place_fields, place_target, place_rule
                if kind:
                    node.update(entity_type=kind, original_entity_type=entity['entity_type'], **fields)
                    if kind == 'website':
                        node['name'] = website_domain(display)
                if target is not None and target != identity and identity not in curated:
                    curated = {**curated, identity: (target, {'confidence': 'exact', 'reason': rule})}
            elif node['entity_type'] == 'person' and not is_parse_artifact(display):
                target, kind, fields, rule = index.cross_type(display, 'person', exclude={identity})
                if target is None and kind:
                    node.update(entity_type=kind, original_entity_type=entity['entity_type'], **fields)
                if target is not None and identity not in curated:
                    curated = {**curated, identity: (target, {'confidence': 'exact', 'reason': rule})}
        prepared.append(node)
    entities = prepared
    groups = defaultdict(list)
    for entity in entities:
        name = clean_display_name(entity['name']) if entity['entity_type'] == 'person' else entity['name']
        groups[(entity['entity_type'], name_key(name))].append(entity)
    # Union exact-name groups with reviewed merges between KG IDs.
    group_of = {e['id']: key for key, records in groups.items() for e in records}
    for member, (canonical, item) in curated.items():
        if isinstance(member, int) and isinstance(canonical, int) and member in group_of and canonical in group_of:
            source, target = group_of[member], group_of[canonical]
            if source != target:
                groups[target].extend(groups.pop(source))
                for e in groups[target]:
                    group_of[e['id']] = target
    named = {}  # canonical -> display_name of a reviewed merge into it (first wins)
    for _, item in curated.values():
        if item.get('display_name'):
            named.setdefault(item['canonical'], item['display_name'])
    mapping, normalized, merges = {}, [], []
    for (kind, key), records in groups.items():
        records = sorted(records, key=lambda e: e['id'])
        # Canonical: the reviewed target if present, else the lowest original ID.
        targets = {final_target(curated, r['id']) for r in records if r['id'] in curated and isinstance(curated[r['id']][0], int)}
        canonical = next((r for r in records if r['id'] in targets), records[0])
        ids = [r['id'] for r in records]
        for i in ids:
            mapping[i] = canonical['id']
        node = dict(canonical)
        display = clean_display_name(canonical['name']) if kind == 'person' else clean_label(canonical['name'])
        display = next((curated[i][1]['display_name'] for i in ids if i in curated and curated[i][1].get('display_name')), display)
        # A reviewed merge whose canonical is this node (members may be addresses) can name it too.
        display = next((named[i] for i in ids if i in named), display)
        node.update(name=display, normalized_name=name_key(display), original_entity_ids=ids,
                    original_names=list(dict.fromkeys(originals[r['id']]['name'] for r in records)),
                    original_records=[originals[r['id']] for r in records])
        verdict = review.get(str(canonical['id']), {}).get('verdict')
        if canonical['id'] not in protected and kind == 'person' and (is_parse_artifact(canonical['name']) or verdict in ('boilerplate', 'ocr_garbage', 'redaction_or_placeholder')):
            node['suspected_parse_artifact'] = True
            node['label_quality'] = 'not_a_name'
            node['label_quality_reason'] = 'flight_log_parse_artifact' if is_parse_artifact(canonical['name']) else 'reviewed_rejection'
        reason = text_reason(display) if canonical['id'] not in protected else None
        if canonical['id'] not in protected and kind in ('organization', 'shell_company') and has_company_form(display):
            reason = index.company_reason(display) or reason
        if reason:
            node['label_quality'] = 'not_a_name'
            node['label_quality_reason'] = reason
        if kind == 'person' and verdict == 'group_or_role':
            node['label_quality'] = 'group_or_role'  # e.g. "Secret Service (4)", "Bodyguard ?"
        normalized.append(node)
        if len(ids) > 1:
            reviewed = [curated[i][1] for i in ids if i in curated]
            merges.append({'canonical_id': canonical['id'], 'entity_type': kind,
                           'normalized_name': node['normalized_name'], 'original_ids': ids,
                           'original_names': node['original_names'],
                           'rule': '; '.join(dict.fromkeys(f"reviewed ({r['confidence']}): {r['reason']}" for r in reviewed))
                                   or 'exact NFC/casefold/whitespace name and entity type (after flight-log name repair)'})
    edges = []
    for rel in relationships:
        edge = dict(rel)
        edge['original_source_entity_id'] = rel['source_entity_id']
        edge['original_target_entity_id'] = rel['target_entity_id']
        edge['source_entity_id'] = mapping[rel['source_entity_id']]
        edge['target_entity_id'] = mapping[rel['target_entity_id']]
        edges.append(edge)  # Do not aggregate weights or discard parallel records.
    return normalized, edges, merges


def main():
    from entity_build_evidence import LabelEvidence
    from airports import Airports
    raw, out = ROOT / 'data/raw', ROOT / 'output'
    entities = json.loads((raw / 'knowledge_graph_entities.json').read_text())
    relationships = json.loads((raw / 'knowledge_graph_relationships.json').read_text())
    # Normalization precedes company extraction. Reading its mention DB here
    # would introduce a dependency from this step onto a later build output.
    nodes, edges, merges = normalize(entities, relationships, evidence=LabelEvidence.project(include_counts=False), airports=Airports())
    out.mkdir(exist_ok=True)
    for name, value in [('normalized_entities.json', nodes), ('normalized_relationships.json', edges), ('entity_merges.json', merges)]:
        (out / name).write_text(json.dumps(value, indent=2) + '\n')
    print(f'Normalized {len(entities)} → {len(nodes)} entities; {len(merges)} merge groups; retained all {len(edges)} relationship records.')


if __name__ == '__main__':
    main()
