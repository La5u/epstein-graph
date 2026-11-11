"""Stream the private reveal table and clean viewer labels before encryption.

The full reveal table is much larger than the viewer. Decode one entry at a
time, keeping only selected IDs. No private values are written in plaintext.
"""
import gzip
import hashlib
import json
import os
from pathlib import Path

from entity_build_rules import clean_label, text_reason
import viewer_secret as secret_api

LABEL_REVIEW = Path(__file__).resolve().parents[1] / 'config/label_review.json'
RETYPES = {'person', 'organization', 'place', 'vehicle'}


def label_signature(label):
    """Short hash of a real label: config/label_review.json holds IDs and hashes, never names."""
    return hashlib.sha256(str(label).encode()).hexdigest()[:10]


def reviewed_labels(path=LABEL_REVIEW):
    """{viewer id: {'v': 'not_a_name' | 'retype', 'type': ..., 'sig': ...}} from the model label review."""
    path = Path(path)
    return json.loads(path.read_text()).get('labels', {}) if path.is_file() else {}


def reviewed_resolutions(entities, labels, review=None):
    """Explicit ID-only folds for extracted labels, bound to both real spellings.

    Run after public_names: the canonical keeps its publication decision and
    only the source's safe display label enters public merge metadata.
    Header identities use identity_merges instead; these are viewer-only folds.
    """
    if review is None:
        review = json.loads(LABEL_REVIEW.read_text()).get('merges', {}) if LABEL_REVIEW.is_file() else {}
    nodes = {str(e['id']): e for e in entities}
    result = {}
    for source, item in review.items():
        target = str(item['target'])
        a, b = nodes.get(str(source)), nodes.get(target)
        if (not a or not b or source == target
                or any(n.get('entity_type') == 'survivor' or n.get('dual_status') for n in (a, b))):
            continue
        if (item.get('sig') != label_signature(labels.get(str(source), a['name']))
                or item.get('target_sig') != label_signature(labels.get(target, b['name']))):
            continue
        result[a['id']] = (b['id'], 'reviewed_label_merge')
    return result


def include_reviewed_targets(entities, graph_path, review=None):
    """Bring existing off-slice targets through survivor/public-name protection.

    Bind both spellings before adding anything. Private spellings are read only
    into this temporary hash-check table; a missing/stale reference adds no node.
    """
    if review is None:
        review = json.loads(LABEL_REVIEW.read_text()).get('merges', {}) if LABEL_REVIEW.is_file() else {}
    nodes = {str(e['id']): e for e in entities}
    pending = {i: item for i, item in review.items() if i in nodes and str(item['target']) not in nodes}
    if not pending:
        return entities
    from bounded_json import items
    wanted = {str(item['target']) for item in pending.values()}
    references = {str(e['id']): e for e in items(graph_path) if str(e['id']) in wanted}
    labels = {i: e['name'] for i, e in (nodes | references).items()}
    needed = pending.keys() | wanted
    if secret_api.SECRET.is_file():
        for table, i, value in secret_entries(secret_api.SECRET):
            if table == 'reveal' and i in needed and value.get('value'):
                labels[i] = value['value']
    out = list(entities)
    for i, item in pending.items():
        target = str(item['target'])
        node = references.get(target)
        if (node and target not in nodes
                and not any(n.get('entity_type') == 'survivor' or n.get('dual_status') for n in (nodes[i], node))
                and item.get('sig') == label_signature(labels[i])
                and item.get('target_sig') == label_signature(labels[target])):
            out.append(node)
            nodes[target] = node
    return out


def secret_entries(path, chunk_size=65536):
    """Yield (table, key, value) from the reveal/pages/reasons JSON maps."""
    decoder = json.JSONDecoder()
    with Path(path).open() as stream:
        buffer, position, eof = '', 0, False

        def fill():
            nonlocal buffer, eof
            chunk = stream.read(chunk_size)
            buffer += chunk
            eof = not chunk

        def peek():
            nonlocal buffer, position
            while True:
                while position < len(buffer) and buffer[position].isspace():
                    position += 1
                if position < len(buffer):
                    return buffer[position]
                if eof:
                    raise ValueError('Unexpected end of private reveal JSON')
                buffer, position = '', 0
                fill()

        def take(character):
            nonlocal position
            if peek() != character:
                raise ValueError('Malformed private reveal JSON')
            position += 1

        def decode():
            nonlocal buffer, position
            peek()
            buffer, position = buffer[position:], 0
            while True:
                try:
                    value, end = decoder.raw_decode(buffer)
                    # A numeric token at a chunk boundary may be incomplete.
                    if end == len(buffer) and not eof:
                        fill()
                        continue
                    position = end
                    return value
                except json.JSONDecodeError:
                    if eof:
                        raise
                    fill()

        take('{')
        while peek() != '}':
            table = decode()
            take(':')
            take('{')
            while peek() != '}':
                key = decode()
                take(':')
                yield table, key, decode()
                if peek() != ',':
                    break
                take(',')
            take('}')
            if peek() != ',':
                break
            take(',')
        take('}')


def early_retypes(entities, review=None):
    """Reviewed retypes to person run before the public-names layer, so a person found
    under an organisation/place type gets a pseudonym like any other private person.
    Their labels are real at that stage (organisation and place names are not encrypted)."""
    review = reviewed_labels() if review is None else review
    out = []
    for e in entities:
        verdict = review.get(str(e['id']))
        if (verdict and verdict.get('v') == 'retype' and verdict.get('type') == 'person'
                and e.get('entity_type') in {'organization', 'place', 'location'}
                and verdict.get('sig') == label_signature(e['name'])):
            e = {**e, 'entity_type': 'person', 'type_resolution_rule': 'reviewed_label_type'}
        out.append(e)
    return out


def label_payload(entities, html_path, secret=True, resolutions=None, airports=None, gazetteer=None, reference_out=None, protected=None, labels_out=None, contact_kinds_out=None):
    """Return nodes, encrypted reveal and unclassified contact values.

    The caller must apply contact_publication.publish before rendering the
    contact map; raw contacts here are build-only inputs, never a public table.
    """
    available = secret_api.SECRET.is_file()
    wanted = set(map(str, secret_api.viewer_ids(entities)))
    node_ids = {str(e['id']) for e in entities}
    payload = {'reveal': {}, 'pages': {}, 'reasons': {}}
    reasons, contacts, labels = {}, {}, {}
    base = os.path.relpath(secret_api.PRIVATE, Path(html_path).resolve().parent)
    entries = secret_entries(secret_api.SECRET) if available else ()
    for table, key, value in entries:
        if table == 'reveal' and key in wanted:
            name = value['value']
            labels[key] = name
            reason = text_reason(name)
            if reason:
                reasons[key] = reason
            kind=key.split('_',1)[0] if key.startswith(secret_api.PUBLIC_KINDS) else value.get('kind')
            if kind in {'email','phone'}:
                contacts[key] = name
                if contact_kinds_out is not None: contact_kinds_out[key]=kind
            if secret:
                payload['reveal'][key] = {**value, 'value': clean_label(name)}
        elif secret and table == 'pages' and key in node_ids:
            payload['pages'][key] = f'{base}/{value}'
        elif secret and table == 'reasons' and secret_api.reason_wanted(key, wanted):
            payload['reasons'][key] = value
    # Newly retyped/private people are protected in this invocation, so they
    # may not yet have an entry in the on-disk reveal table. Keep their real
    # spelling in memory for hash checks, including --public builds.
    for identity, entry in (protected or {}).items():
        if entry.get('value'):
            labels.setdefault(str(identity), entry['value'])
    if labels_out is not None:
        labels_out.update(labels)
    if secret:
        entity_names = {str(e['id']):e['name'] for e in entities}
        for identity, entry in (protected or {}).items():
            previous = payload['reveal'].get(identity, {})
            payload['reveal'][identity] = {**previous, **entry,
                'fields': {**previous.get('fields', {}), **entry.get('fields', {})}}
            payload['reveal'][identity].setdefault('value', entity_names.get(identity, identity))
            # Incidental metadata protection may carry a node's pseudonym.
            # It must not overwrite that contact's actual private value.
            if previous.get('value') and (identity.startswith(secret_api.PUBLIC_KINDS)
                    or previous.get('kind') in {'email','phone'}):
                payload['reveal'][identity]['value'] = previous['value']
    review = reviewed_labels()

    def annotate(node):
        reason = reasons.get(str(node['id'])) or text_reason(node['name'])
        result = {**node, 'name': clean_label(node['name'])}
        verdict = review.get(str(node['id']))
        # A verdict holds only for the label it was given for (same real spelling).
        if verdict and verdict.get('sig') != label_signature(labels.get(str(node['id']), node['name'])):
            verdict = None
        if verdict and node.get('entity_type') != 'survivor':
            if verdict.get('v') == 'not_a_name' and not reason:
                reason = 'reviewed_not_a_name'
            # Participants stay participants (hidden until unlock); only drawn types are retyped.
            # Retypes to person ran earlier (early_retypes); people retyped away keep their pseudonym.
            elif (verdict.get('v') == 'retype' and verdict.get('type') in RETYPES - {'person'}
                  and node.get('entity_type') != 'participant'):
                result.update(entity_type=verdict['type'], type_resolution_rule='reviewed_label_type')
        if reason and node.get('entity_type') != 'survivor':
            result.update(label_quality='not_a_name', label_quality_reason=reason)
        if node.get('expand'):
            result['expand'] = [annotate(x) for x in node['expand']]
        return result
    entities = [annotate(e) for e in entities]
    if resolutions is not None:
        # Private labels are used only in this temporary index. Public nodes,
        # aliases and merge pointers below retain their pseudonymous spellings.
        from entity_build_resolution import BuildIndex, viewer_references, website_domain
        from entity_build_evidence import LabelEvidence, DocumentOverlap
        from entity_build_rules import website_reason
        from entity_labels import dictionary_words
        references = viewer_references(reference_out, [*labels.values(), *(e['name'] for e in entities)], gazetteer) if reference_out else {}
        brand_evidence = LabelEvidence.project() if reference_out else LabelEvidence()
        if reference_out:
            brand_evidence.prepare_reference_brands(references)
            brand_evidence.prepare_domains({d for value in [*labels.values(), *(e['name'] for e in entities)]
                                            if (d := website_domain(value))})
        indexed = {**references, **{e['id']: {**e, 'name': labels.get(str(e['id']), e['name']),
                            'entity_type': 'location' if e['entity_type'] == 'place' else e['entity_type']}
                   for e in entities}}
        overlap = DocumentOverlap(reference_out / 'documents.db') if reference_out else None
        index = BuildIndex(indexed, gazetteer, airports=airports, evidence=brand_evidence, document_overlap=overlap)
        resolved = []
        dictionary = dictionary_words()
        for node in entities:
            spelling = indexed[node['id']]['name']
            if node['entity_type'] in {'organization', 'shell_company', 'participant'} and node.get('label_quality') != 'not_a_name':
                reason = index.stub_reason(spelling, dictionary)
                if reason:
                    node = {**node, 'label_quality': 'not_a_name', 'label_quality_reason': reason}
                    resolved.append(node)
                    continue
            if node['entity_type'] in {'organization', 'shell_company'} and node.get('label_quality') != 'not_a_name':
                cleaned, company_target, company_rule = index.company_repair(spelling, exclude={node['id']})
                if company_target is not None:
                    resolutions[node['id']] = (company_target, company_rule)
                    resolved.append(node)
                    continue
                if cleaned != spelling:
                    if node['name'] == spelling:
                        node = {**node, 'name': cleaned}
                    if secret and str(node['id']) in payload['reveal']:
                        payload['reveal'][str(node['id'])]['value'] = cleaned
            target, kind, fields, rule = index.cross_type(spelling, node['entity_type'], exclude={node['id']})
            if kind and node.get('label_quality') != 'not_a_name':
                if target is not None:
                    resolutions[node['id']] = (target, rule)
                else:
                    node = {**node, 'entity_type': kind, **fields, 'type_resolution_rule': rule}
                    if kind == 'website':
                        domain = website_domain(spelling)
                        reason = website_reason(domain, (spelling,), dictionary, domain in brand_evidence.domains)
                        if reason:
                            node.update(label_quality='not_a_name', label_quality_reason=reason)
            resolved.append(node)
        entities = resolved
        if overlap:
            overlap.close()
        # Explicit label review takes precedence over heuristic display repair.
        resolutions.update(reviewed_resolutions(entities, labels))
        present = {n['id'] for n in entities}
        for target, _ in resolutions.values():
            if target not in present and target in references:
                entities.append(dict(references[target]))
                present.add(target)
    encrypted = None
    if secret and available and secret_api.PASSWORD.is_file():
        password = secret_api.PASSWORD.read_text().strip()
        if len(password) < secret_api.MIN_LENGTH:
            raise SystemExit('Viewer password is too short; set a longer one with --set-password')
        compressed = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0)
        encrypted = {**secret_api.encrypt(compressed, password), 'compression': 'gzip'}
    return entities, encrypted, contacts


def fold_viewer_resolutions(entities, relationships, resolutions, never=None):
    """Collapse exact display duplicates; full-graph nodes and evidence stay intact."""
    nodes = {n['id']: dict(n) for n in entities}
    if never is None:
        from build_communications import never_merge_pairs
        never = never_merge_pairs()
    identity = lambda i: str(i).removeprefix('kg:')
    blocked = [set(map(identity, pair)) for pair in never]
    members = {i: {identity(i), *(identity(m['id']) for m in n.get('merged_identities', []) if 'id' in m),
                   *(identity(m) for m in n.get('original_entity_ids', []))} for i, n in nodes.items()}
    mapping = {}
    for source, (target, rule) in resolutions.items():
        if source == target or source not in nodes or target not in nodes:
            continue
        # Frozen targets prevent chains/cycles; check entire accumulated clusters.
        if (target in resolutions
                or any(nodes[i]['entity_type'] == 'survivor' or nodes[i].get('dual_status') for i in (source, target))
                or any(pair <= members[source] | members[target] for pair in blocked)):
            continue
        mapping[source] = target
        members[target] |= members[source]
        node = nodes[target]
        node['merged_identities'] = [*node.get('merged_identities', []),
            {'id': source, 'value': nodes[source]['name'], 'rule': rule, 'confidence': 'exact'},
            *nodes[source].get('merged_identities', [])]
    edges = []
    for relationship in relationships:
        edge = dict(relationship)
        for field in ('source_entity_id', 'target_entity_id'):
            if edge[field] in mapping:
                edge.setdefault('original_' + field, edge[field])
                edge[field] = mapping[edge[field]]
        if edge['source_entity_id'] != edge['target_entity_id']:
            edges.append(edge)
    return [node for identity, node in nodes.items() if identity not in mapping], edges
