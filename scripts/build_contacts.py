"""Add HMAC-pseudonymized contacts and explicit, unverified shared-reference links.

No ownership/communication inference. No raw contacts or reverse mapping are written.
The local HMAC key stays under ignored data/private, outside viewer output.
"""
import hashlib
import hmac
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from normalize_graph import main as normalize_main, name_key
from legal_status import load_annotations
from victim_policy import dual_status_ids, extra_nodes as victim_extra_nodes, victim_ids

ROOT = Path(__file__).resolve().parents[1]


def normalize_contact(value, kind):
    value = value.strip()
    if kind == 'email':
        if value.count('@') != 1 or re.search(r'\s', value):
            return None
        local, domain = value.rsplit('@', 1)
        # Preserve local-part case; only domain names are case-insensitive.
        return local + '@' + domain.casefold() if local and domain else None
    if not re.fullmatch(r'\+?[\d\s().-]+', value, flags=re.ASCII):
        return None
    digits = re.sub(r'\D', '', value)
    if not 7 <= len(digits) <= 15 or placeholder_number(digits):
        return None
    # North American numbers (10 digits, area code and exchange 2-9, optionally
    # with the leading 1) are written +1XXXXXXXXXX whether or not the source had
    # a "+". Other numbers keep their prefix as written: no country-code guessing.
    national = digits[1:] if len(digits) == 11 and digits[0] == '1' else digits
    if len(national) == 10 and national[0] in '23456789' and national[3] in '23456789':
        return '+1' + national
    return ('+' if value.startswith('+') else '') + digits


def placeholder_number(digits):
    """Filler, not a phone: one repeated digit, mostly zeros, or a counting run."""
    return (len(set(digits)) <= 2 and digits.count(digits[0]) >= len(digits) - 1) or digits.count('0') >= len(digits) - 3 or digits.startswith('0000') \
        or digits in '01234567890123456789' or digits in '98765432109876543210'


def one_digit_apart(a, b):
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def contact_id(value, kind, key):
    return kind + '_' + hmac.new(key, (kind + '\0' + value).encode(), hashlib.sha256).hexdigest()[:24]


def build(entities, export, key):
    candidates = defaultdict(list)
    for entity in entities:
        kind = entity['entity_type']
        if kind not in ('person', 'organization', 'shell_company'):
            continue
        # Single-token personal labels are too ambiguous for new contact links.
        if kind == 'person' and len(entity['normalized_name'].split()) < 2:
            continue
        group = 'names' if kind == 'person' else 'organizations'
        candidates[(group, entity['normalized_name'])].append(entity)
    by_ref = defaultdict(set)
    for group in ('names', 'organizations'):
        for row in export[group]:
            matches = candidates.get((group, name_key(row['entity_value'])), [])
            if len(matches) != 1:
                continue
            for ref in row['efta_numbers']:
                by_ref[ref].add(matches[0]['id'])
    contacts, skipped, values = {}, defaultdict(int), {}
    for group, kind in [('emails', 'email'), ('phones', 'phone')]:
        for row in export[group]:
            value = normalize_contact(row['entity_value'], kind)
            if value is None:
                skipped[kind] += 1
                continue
            identity = contact_id(value, kind, key)
            values[identity] = value  # memory only
            if identity not in contacts:
                contacts[identity] = {'id': identity, 'name': kind.title() + '_' + identity.split('_', 1)[1],
                                      'entity_type': kind, 'efta_numbers': set(), 'source_record_count': 0,
                                      'max_reported_document_count': 0}
            node = contacts[identity]
            node['efta_numbers'].update(row['efta_numbers'])
            node['source_record_count'] += 1
            node['max_reported_document_count'] = max(node['max_reported_document_count'], row['document_count'])
    edges, linked = [], set()
    for node in contacts.values():
        references = sorted(node['efta_numbers'])
        node['efta_numbers'] = references
        evidence = defaultdict(list)
        for ref in references:
            for entity_id in by_ref.get(ref, ()):
                evidence[entity_id].append(ref)
        for entity_id, refs in sorted(evidence.items()):
            linked.add(node['id'])
            edges.append({'id': f'contact_ref_{entity_id}_{node["id"]}',
                          'source_entity_id': entity_id, 'target_entity_id': node['id'],
                          'relationship_type': 'shares_exported_efta_reference', 'weight': len(refs),
                          'date_first': None, 'date_last': None,
                          'metadata': {'source': 'extracted_entities_filtered.json', 'efta_numbers': refs,
                                       'meaning': 'Name label and contact share exported EFTA references; identity match unverified. NOT ownership, communication, or proof of wrongdoing.',
                                       'name_match_rule': 'Exact normalized multi-token person label or organization label',
                                       'coverage': 'Exported references only; not a complete corpus join.'}})
    # Phones one digit apart (an OCR misread: 5615555891 / 5615550650) that share a
    # linked person fold into the number with more references.
    linked_to = defaultdict(set)
    for edge in edges:
        linked_to[edge['target_entity_id']].add(edge['source_entity_id'])
    phones = sorted((n for n in contacts.values() if n['entity_type'] == 'phone' and linked_to.get(n['id'])),
                    key=lambda n: (-len(n['efta_numbers']), n['id']))
    for i, keep in enumerate(phones):
        for other in phones[i + 1:]:
            if other['id'] in contacts and keep['id'] in contacts and other['id'] != keep['id'] \
                    and linked_to[keep['id']] & linked_to[other['id']] and one_digit_apart(values[keep['id']], values[other['id']]):
                keep.setdefault('merged_identities', []).append({'id': other['id'], 'rule': 'phone_one_digit_variant_same_person', 'confidence': 'probable',
                                                                 'efta_numbers': other['efta_numbers']})
                keep['efta_numbers'] = sorted(set(keep['efta_numbers']) | set(other['efta_numbers']))
                del contacts[other['id']]
                for edge in edges:
                    if edge['target_entity_id'] == other['id']:
                        edge['target_entity_id'] = keep['id']
                        edge['id'] = f"contact_ref_{edge['source_entity_id']}_{keep['id']}"
    merged_edges = {}
    for edge in edges:  # two variants linked to one person become one edge
        if edge['id'] in merged_edges:
            kept = merged_edges[edge['id']]
            kept['metadata']['efta_numbers'] = sorted(set(kept['metadata']['efta_numbers']) | set(edge['metadata']['efta_numbers']))
            kept['weight'] = len(kept['metadata']['efta_numbers'])
        else:
            merged_edges[edge['id']] = edge
    edges = list(merged_edges.values())
    linked = {e['target_entity_id'] for e in edges}
    all_nodes = sorted(contacts.values(), key=lambda n: n['id'])
    graph_nodes = [n for n in all_nodes if n['id'] in linked]
    summary = {'pseudonymous_contacts': len(all_nodes), 'linked_contacts_shown': len(graph_nodes),
               'contact_reference_edges': len(edges), 'unsupported_records_skipped': dict(skipped),
               'contact_types': {kind: sum(n['entity_type'] == kind for n in all_nodes) for kind in ('email', 'phone')},
               'warning': 'No raw contact values exported. Unlinked contacts omitted from viewer; all pseudonyms in contact_nodes.json. Shared reference does not imply contact ownership or communication.'}
    return all_nodes, graph_nodes, edges, summary


def contact_key():
    """Return the local key, preserving any pinned/reviewed pseudonym namespace."""
    secret = ROOT / 'data/private/contact_hmac.key'
    pin = ROOT / 'config/contact_key_fingerprint.json'
    expected = None
    if pin.exists():
        expected = json.loads(pin.read_text()).get('sha256')
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected):
            raise ValueError('Invalid sha256 in config/contact_key_fingerprint.json')
    if not secret.exists():
        if expected is not None:
            raise ValueError('Pinned contact HMAC key is missing; restore the original private key before building.')
        # Review files can predate the fingerprint pin. Do not silently create a
        # different namespace for their pseudonymous IDs either.
        for name in ('identity_merges.json', 'node_review.json'):
            review = ROOT / 'config' / name
            if review.exists() and re.search(r'"(?:email|phone|participant|address)_[^"\s]+"', review.read_text()):
                raise ValueError('Contact HMAC key is missing but pseudonymous reviews exist; restore the original key or explicitly reinitialize the reviewed namespace.')
        secret.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(os.urandom(32))
    key = secret.read_bytes()
    if expected is not None and not hmac.compare_digest(hashlib.sha256(key).hexdigest(), expected.lower()):
        raise ValueError('Contact HMAC key fingerprint mismatch; restore the original private key before building.')
    secret.chmod(0o600)
    return key


def email_places_available():
    """Invalidate optional cached exports when their private input is absent."""
    if (ROOT / 'data/private/email_places.db').is_file():
        return True
    out = ROOT / 'output'
    out.mkdir(parents=True, exist_ok=True)
    for name in ('email_place_nodes.json', 'email_place_edges.json'):
        (out / name).write_text('[]\n')
    return False


def safe_identity_rule(rule):
    # Older identity maps embedded private review reasons after this prefix.
    if rule.startswith('reviewed'):
        return 'reviewed_audit_merge' if rule.startswith('reviewed_audit') else 'reviewed_identity_merge'
    return rule


def apply_identity_map(nodes, edges, path):
    """Fold merged pseudonymous contacts into their canonical identity
    (map written by build_communications; rules recorded on the canonical node)."""
    if not path.exists():
        return nodes, edges
    items = json.loads(path.read_text())
    # A member shown (e.g. an exported contact address) whose canonical identity is not in the
    # view brings the canonical in, so the address still folds into its person.
    for member, item in items.items():
        canonical = item['canonical']
        if member in nodes and canonical not in nodes and isinstance(canonical, str) and '_' in canonical:
            kind, digest = canonical.split('_', 1)
            nodes[canonical] = {'id': canonical, 'entity_type': kind if kind in ('participant', 'email', 'phone') else nodes[member]['entity_type'],
                                'name': {'participant': 'HeaderLabel_', 'email': 'Email_', 'phone': 'Phone_'}.get(kind, '') + digest,
                                'source': nodes[member].get('source', '')}
    mapping = {member: item for member, item in items.items() if item['canonical'] in nodes}
    merged_ids = {}  # canonical -> ids already listed; sanitize each list once, not per member
    for member, item in mapping.items():
        canonical = nodes[item['canonical']]
        merged = canonical.setdefault('merged_identities', [])
        ids = merged_ids.get(item['canonical'])
        if ids is None:
            for previous in merged:
                if 'rule' in previous:
                    previous['rule'] = safe_identity_rule(previous['rule'])
            ids = merged_ids[item['canonical']] = {m['id'] for m in merged}
        if member not in ids:
            ids.add(member)
            merged.append({'id': member, 'rule': safe_identity_rule(item['rule']), 'confidence': item['confidence'],
                           **{k: item[k] for k in ('source_documents', 'ocr_artifacts') if k in item}})
        nodes.pop(member, None)
    remapped = []
    for edge in edges:
        a = mapping.get(edge['source_entity_id'], {}).get('canonical', edge['source_entity_id'])
        b = mapping.get(edge['target_entity_id'], {}).get('canonical', edge['target_entity_id'])
        if a != edge['source_entity_id'] or b != edge['target_entity_id']:
            if a == b and edge['relationship_type'] in ('header_label_association', 'upstream_resolved_header_identity', 'shares_exported_efta_reference'):
                continue  # identity link absorbed by the merge
            edge = {**edge, 'source_entity_id': a, 'target_entity_id': b,
                    'original_endpoint_ids': [edge['source_entity_id'], edge['target_entity_id']]}
        remapped.append(edge)
    return nodes, remapped


def apply_victim_type(nodes, relationships=()):
    """Render curated survivor/victim-labelled entities as the separate 'survivor'
    type, and add any configured victim name with no upstream node as a
    standalone node. No new identities are inferred from the corpus."""
    ids = victim_ids(list(nodes.values()), relationships=relationships,
                     config=ROOT / 'config/victims.json',
                     survivor_config=ROOT / 'config/survivors.json',
                     registry=ROOT / 'data/raw/persons_registry.json')
    for entity_id in ids:
        node = nodes.get(entity_id)
        if not node:
            continue
        node['original_entity_type'] = node.get('entity_type')
        node['entity_type'] = 'survivor'
        node.pop('suspected_parse_artifact', None)
        node.pop('label_quality', None)
    for entity_id in dual_status_ids(list(nodes.values()), survivor_config=ROOT / 'config/survivors.json') - ids:
        nodes[entity_id]['dual_status'] = True  # drawn half survivor red in the viewer
    for extra in victim_extra_nodes(ROOT / 'config/victims.json'):
        node_id = 'victim_' + re.sub(r'\W+', '_', extra['name'].casefold()).strip('_')
        names = [extra['name'], *extra.get('aliases', [])]
        existing = {name_key(v) for node in nodes.values()
                    for v in (node.get('name', ''), *node.get('original_names', [])) if v}
        if node_id in nodes or any(name_key(n) in existing for n in names):
            continue
        nodes[node_id] = {'id': node_id, 'name': extra['name'], 'entity_type': 'survivor',
                          'normalized_name': name_key(extra['name']), 'original_names': names,
                          'source': extra.get('source'), 'note': extra.get('note'),
                          'efta_numbers': [extra['efta']] if extra.get('efta') else [],
                          'added_by': 'config/victims.json'}
    return ids


def main():
    key = contact_key()  # fail before generating outputs if the namespace changed
    has_email_places = email_places_available()
    normalize_main()
    out = ROOT / 'output'
    entities = json.loads((out / 'normalized_entities.json').read_text())
    relationships = json.loads((out / 'normalized_relationships.json').read_text())
    export = json.loads((ROOT / 'data/raw/extracted_entities_filtered.json').read_text())
    all_nodes, graph_nodes, edges, summary = build(entities, export, key)
    combined_nodes = {n['id']: n for n in entities + graph_nodes}
    combined_edges = relationships + edges
    communication_nodes = out / 'communication_nodes.json'
    communication_edges = out / 'communication_edges.json'
    if communication_nodes.exists() and communication_edges.exists():
        for node in json.loads(communication_nodes.read_text()):
            combined_nodes[node['id']] = {**combined_nodes.get(node['id'], {}), **node}
        combined_edges.extend(json.loads(communication_edges.read_text()))
    for places in ('place_nodes.json', 'email_place_nodes.json'):  # build_places.py, build_email_places.py
        if (places != 'email_place_nodes.json' or has_email_places) and (out / places).exists():
            for node in json.loads((out / places).read_text()):
                canonical = combined_nodes.setdefault(node['id'], node)
                # Email evidence supplements existing public properties without
                # replacing their canonical label, type or source metadata.
                for field in ('email_document_count', 'merged_address_aliases'):
                    if field in node:
                        canonical[field] = node[field]
    if (out / 'victim_nodes.json').exists():  # build_victims.py: censored designators
        for node in json.loads((out / 'victim_nodes.json').read_text()):
            combined_nodes.setdefault(node['id'], {**node, 'entity_type': 'survivor'})
    if (out / 'company_nodes.json').exists():  # build_companies.py: reviewed full-text companies
        for node in json.loads((out / 'company_nodes.json').read_text()):
            canonical = combined_nodes.setdefault(node['id'], node)
            if 'relevance' in node:
                canonical['relevance'] = node['relevance']
    if (out / 'entity_nodes.json').exists():
        for node in json.loads((out / 'entity_nodes.json').read_text()):
            canonical = combined_nodes.setdefault(node['id'], node)
            for field in ('subtype',):
                if field in node:
                    canonical[field] = node[field]
    if (out / 'vehicle_nodes.json').exists():  # build_vehicles.py: evidence and reviewed labels
        for node in json.loads((out / 'vehicle_nodes.json').read_text()):
            combined_nodes[node['id']] = {**combined_nodes.get(node['id'], {}), **node}
    # Keep the KG IDs, including when the optional vehicle layer is not built yet.
    for node in combined_nodes.values():
        if node['entity_type'] == 'aircraft':
            node['entity_type'] = 'vehicle'
            node['vehicle_type'] = 'aircraft'
    known = set(combined_nodes)
    for extra in ('comention_edges.json', 'copresence_edges.json', 'place_edges.json',
                  'email_place_edges.json', 'victim_edges.json', 'company_edges.json', 'vehicle_edges.json', 'entity_edges.json'):
        if (extra != 'email_place_edges.json' or has_email_places) and (out / extra).exists():
            layer_edges = [e for e in json.loads((out / extra).read_text())
                           if e['source_entity_id'] in known and e['target_entity_id'] in known]
            if extra == 'vehicle_edges.json':
                # Context-qualified vehicle evidence replaces older plain-name
                # co-mentions for the same pair; do not count both layers twice.
                pairs = {frozenset((e['source_entity_id'], e['target_entity_id'])) for e in layer_edges
                         if e['relationship_type'] == 'co_mentioned_in_documents'}
                combined_edges = [e for e in combined_edges if e['relationship_type'] != 'co_mentioned_in_documents'
                                  or frozenset((e['source_entity_id'], e['target_entity_id'])) not in pairs]
            combined_edges.extend(layer_edges)
    combined_nodes, combined_edges = apply_identity_map(combined_nodes, combined_edges, out / 'identity_map.json')
    apply_victim_type(combined_nodes, combined_edges)
    load_annotations(list(combined_nodes.values()))
    for name, value in [('contact_nodes.json', all_nodes), ('contact_summary.json', summary),
                        ('graph_entities.json', list(combined_nodes.values())), ('graph_relationships.json', combined_edges)]:
        (out / name).write_text(json.dumps(value, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
