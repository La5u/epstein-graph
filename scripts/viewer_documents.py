"""Document-backed last-resort viewer links, without scanning corpus text.

Runs inside the traced visualize_graph step. All public evidence contains only
node IDs and document IDs; real contact values are used in memory for FTS only.
"""
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import corpus

ROOT = Path(__file__).resolve().parents[1]
NON_NAMES = {'group_or_role', 'not_a_name', 'boilerplate', 'ocr_garbage', 'redaction_or_placeholder'}
HIDDEN_PLACES = {'address', 'airport', 'institution', 'city', 'other'}
FTS_LIMIT = 200


def eligible(e):
    return not e.get('suspected_parse_artifact') and e.get('label_quality') not in NON_NAMES


def incidental(e):
    """Hidden by the viewer's 'Show incidental organizations' switch (off in every mode)."""
    meta = e.get('metadata') if isinstance(e.get('metadata'), dict) else {}
    return (e.get('relevance') or meta.get('relevance')) == 'incidental'


def visible(e, mode='default'):
    if not eligible(e) or incidental(e):
        return False
    if mode == 'all_types':
        return True
    if e['entity_type'] == 'place' and e.get('subtype') in HIDDEN_PLACES:
        return False
    return mode == 'unlocked' or e['entity_type'] != 'participant' or bool(e.get('dual_status'))


def usable_edge(r):
    return not (r['relationship_type'] == 'co_mentioned_in_documents'
                and isinstance(r.get('metadata'), dict) and r['metadata'].get('new_link') is False)


def isolates(entities, relationships, mode='default'):
    ids = {e['id'] for e in entities if visible(e, mode)}
    linked = {n for r in relationships if usable_edge(r) and r['source_entity_id'] != r['target_entity_id']
              and r['source_entity_id'] in ids and r['target_entity_id'] in ids
              for n in (r['source_entity_id'], r['target_entity_id'])}
    return ids - linked


def read_documents(entities, out):
    """Complete indexed lists, not the viewer's truncated document samples.

    Stream each index once. Intern repeated document IDs to bound memory; the
    communication endpoints already include per-document identity attributions.
    """
    ids = {str(e['id']): e['id'] for e in entities}
    comm_ids = {('kg:' + k if isinstance(v, int) else k): v for k, v in ids.items()}
    docs = {e['id']: set() for e in entities}

    def add(node, doc):
        if node is not None and doc:
            docs[node].add(sys.intern(doc))

    path = out / 'documents.db'
    if path.exists():
        with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
            for node, doc in db.execute('SELECT node_id, efta FROM node_documents'):
                add(ids.get(node), doc)
    for name in ('comention_graph.db', 'company_graph.db'):
        path = out / name
        if path.exists():
            with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
                for node, doc in db.execute('SELECT entity_id, efta_number FROM mentions'):
                    add(ids.get(str(node)), doc)
    path = out / 'communication_graph.db'
    if path.exists():
        with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
            for a, b, doc, source in db.execute('SELECT canonical_source, canonical_target, efta_number, source_table FROM evidence'):
                doc = doc or ('jmail:' + source[6:] if source.startswith('jmail:') else None)
                add(comm_ids.get(a), doc)
                add(comm_ids.get(b), doc)
    # Full-text contact mentions, including canonical owners and values folded
    # into a person. This index includes body mentions absent from email headers.
    path = out / 'contact_graph.db'
    if path.exists():
        with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
            for raw, canonical, owner, doc in db.execute('SELECT n.id, n.canonical, n.owner, m.efta FROM mentions m JOIN nodes n ON n.id=m.id'):
                for identity in (raw, canonical, owner):
                    add(ids.get(identity), doc)
    path = out / 'victim_documents.json'
    if path.exists():
        for node, documents in json.loads(path.read_text()).items():
            for doc in documents:
                add(ids.get(node), doc)
    path = out / 'vehicle_evidence.json'
    if path.exists():
        for e in json.loads(path.read_text()):
            for doc in e['documents']:
                add(ids.get(str(e['entity_id'])), doc)
    return docs


def page_documents(entities, docs, directory):
    """Reuse node_documents.py's existing header/full-text lists, one file at a time.

    Do not read via-link sections (already in documents.db). Only document IDs
    leave this private input; labels, identity values and notes stay in memory.
    Each file read is tracked by the visualization build's normal audit hook.
    """
    from node_documents import page_name
    for e in entities:
        path = directory / page_name(e['id'])
        if not path.exists():
            continue
        with path.open() as stream:
            for line in stream:
                if line.startswith('<script>const D = '):
                    data, _ = json.JSONDecoder().raw_decode(line[len('<script>const D = '):])
                    for section in data['sections']:
                        if section['id'] in ('header', 'full-text', 'own'):
                            docs[e['id']].update(sys.intern(row[0]) for row in section['rows'])
                    break
    return docs


def missing_documents(entities, wanted, docs, private, db):
    """Bounded exact-phrase FTS only for nodes with no indexed documents."""
    from build_documents import search_names, named
    searched = []
    for e in entities:
        if e['id'] in wanted and not docs[e['id']]:
            names = search_names(e, private)
            if names:
                docs[e['id']].update(named(db, names, limit=FTS_LIMIT))
                searched.append(e['id'])
    return searched


def merge_surnames(entities, relationships, docs):
    """Unique fuller surname + shared document evidence; never guess between people.

    Titles alone do not make a fuller name (Prince Andrew is not a surname match).
    Preserve provenance, edge IDs, and all documents on the surviving node.
    """
    full = defaultdict(list)
    for e in entities:
        words = re.findall(r"[\w'-]+", e['name'])
        if e['entity_type'] == 'person' and eligible(e) and len(words) > 1 and words[0].casefold() not in {'prince', 'sir', 'lord', 'mr', 'mrs', 'ms', 'dr'}:
            full[words[-1].casefold()].append(e)
    merged, by_id = {}, {e['id']: dict(e) for e in entities}
    for e in entities:
        words = re.findall(r"[\w'-]+", e['name'])
        if not (isinstance(e['id'], int) and e['entity_type'] == 'person' and len(e['name'].split()) == 1 and len(words) == 1 and eligible(e)):
            continue
        candidates = full[words[0].casefold()]
        if len(candidates) != 1:
            continue
        target = candidates[0]['id']
        shared = docs[e['id']] & docs[target]
        if not shared:
            continue
        merged[e['id']] = target
        canonical = by_id[target]
        canonical['original_names'] = list(dict.fromkeys([*canonical.get('original_names', [canonical['name']]), e['name'], *e.get('original_names', [])]))
        canonical['original_entity_ids'] = list(dict.fromkeys([*canonical.get('original_entity_ids', [target]), *e.get('original_entity_ids', [e['id']])]))
        canonical['merged_identities'] = [*canonical.get('merged_identities', []), *e.get('merged_identities', []),
            {'id': e['id'], 'rule': 'unique_surname_shared_documents', 'confidence': 'probable',
             'source_documents': [{'efta_number': d} for d in sorted(shared)[:10]]}]
        docs[target].update(docs[e['id']])
    edges = []
    for r in relationships:
        a, b = r['source_entity_id'], r['target_entity_id']
        na, nb = merged.get(a, a), merged.get(b, b)
        if na == nb and a != b:
            continue
        edges.append({**r, 'source_entity_id': na, 'target_entity_id': nb})
    return [by_id[e['id']] for e in entities if e['id'] not in merged], edges, merged


def targets(entities, relationships):
    shown = {e['id'] for e in entities if visible(e)}
    ok = {e['id'] for e in entities if eligible(e)}
    linked, owned = set(), set()
    for r in relationships:
        a, b = r['source_entity_id'], r['target_entity_id']
        # An organization's own website (exact domain/name match) is not a
        # document partner: such pairs otherwise sit alone although the company
        # shares documents with other nodes. The website itself folds into the
        # organization in the viewer (visualize_graph.fold_own_websites).
        if r['relationship_type'] == 'website_of':
            owned.add(a)
            continue
        if a == b or not usable_edge(r) or a not in ok or b not in ok:
            continue
        if b in shown:
            linked.add(a)
        if a in shown:
            linked.add(b)
    return ok - linked - owned


def evidence(documents, limit=None):
    """Same shape as build_documents: PDF documents and non-PDF source records."""
    pdfs = [d for d in documents if not d.startswith('jmail:')]
    messages = [d[6:] for d in documents if d.startswith('jmail:')]
    item = {'n': len(pdfs), 'docs': [{'efta_number': d, 'how': 'in the same document'} for d in pdfs[:limit]]}
    if messages:
        item.update(records_n=len(messages), records=[{'kind': 'jmail email', 'ref': d, 'url': 'https://jmail.world/thread/' + d} for d in messages[:limit]])
    return item


def add_links(entities, relationships, docs, wanted, limit=3):
    by_id = {e['id']: e for e in entities if eligible(e)}
    # Invert only the requested nodes' documents, rather than all pairs in the graph.
    readers = defaultdict(list)
    for node in sorted(wanted, key=str):
        for doc in docs[node]:
            readers[doc].append(node)
    counts = defaultdict(Counter)
    for other in by_id:
        for doc in docs[other]:
            for node in readers.get(doc, ()):
                if node != other:
                    counts[node][other] += 1
    added, pairs = [], {frozenset((r['source_entity_id'], r['target_entity_id'])) for r in relationships if r['relationship_type'] == 'same_document'}
    for node in sorted(wanted, key=str):
        ranked = sorted(counts[node], key=lambda other: (not visible(by_id[other]), -counts[node][other], str(other)))[:limit]
        for other in ranked:
            pair = frozenset((node, other))
            if pair in pairs:
                continue
            pairs.add(pair)
            a, b = sorted(pair, key=str)
            shared = sorted(docs[a] & docs[b])
            edge = 'same_document_' + hashlib.sha256(json.dumps([a, b]).encode()).hexdigest()[:24]
            added.append({'id': edge, 'source_entity_id': a, 'target_entity_id': b,
                          'relationship_type': 'same_document', 'weight': len(shared),
                          'metadata': {'shared_documents': len(shared), 'meaning': 'Both nodes occur in these source documents. A reading lead, not evidence of a personal relationship.'},
                          'documents': evidence(shared, limit=20)})
    return relationships + added, added


def augment(entities, relationships, out=None, private=None):
    out = Path(out or ROOT / 'output')
    private_dir = out.parent / 'data/private'
    modes = ('default', 'unlocked', 'all_types')
    before = {m: len(isolates(entities, relationships, m)) for m in modes}
    docs = read_documents(entities, out)
    page_documents(entities, docs, private_dir / 'node_docs')
    wanted = targets(entities, relationships)
    # A surname can already have links: check it too, before selecting isolates.
    wanted |= {e['id'] for e in entities if isinstance(e['id'], int) and e['entity_type'] == 'person' and len(e['name'].split()) == 1}
    searched = []
    if corpus.PATH.exists():
        if private is None:
            private = {}
            for name in ('contact_lookup.json', 'address_lookup.json'):
                path = private_dir / name
                if path.exists():
                    private.update({k: v['value'] for k, v in json.loads(path.read_text()).items()})
        with closing(corpus.connect()) as db:
            searched = missing_documents(entities, wanted, docs, private, db)
    entities, relationships, merged = merge_surnames(entities, relationships, docs)
    wanted = targets(entities, relationships)
    relationships, added = add_links(entities, relationships, docs, wanted)
    remaining = {m: isolates(entities, relationships, m) for m in modes}
    evidence_items = [r['documents'] for r in added]
    for e in entities:
        if any(e['id'] in ids for ids in remaining.values()):
            e['document_isolation_reason'] = ('no source documents found by indexed evidence or limited exact-phrase search' if not docs[e['id']]
                else 'no other eligible viewer node shares its indexed documents' if not any(e['id'] in (r['source_entity_id'], r['target_entity_id']) and r['source_entity_id'] != r['target_entity_id'] for r in relationships)
                else 'document partners are hidden by this view')
        # Make recovered own documents and merged-node documents available in the panel.
        if docs[e['id']] and (not e.get('documents', {}).get('n') or e['id'] in merged.values()):
            e['documents'] = evidence(sorted(docs[e['id']]), limit=10)
            evidence_items.append(e['documents'])
    # One batch: document_urls reads the compressed URL registry on each call.
    urls = corpus.document_urls({d['efta_number'] for item in evidence_items for d in item['docs']})
    for item in evidence_items:
        for d in item['docs']:
            if d['efta_number'] in urls:
                d['pdf_url'] = urls[d['efta_number']]
    report = {'before': before, 'after': {m: len(ids) for m, ids in remaining.items()},
              'same_document_edges': len(added), 'surname_merges': {str(k): v for k, v in merged.items()},
              'fts_searched_nodes': searched,
              'remaining': {m: [{'id': e['id'], 'reason': e['document_isolation_reason']} for e in entities if e['id'] in ids] for m, ids in remaining.items()}}
    (out / 'viewer_document_links.json').write_text(json.dumps(report, indent=2) + '\n')
    print('Document links:', json.dumps({k: v for k, v in report.items() if k not in ('remaining', 'fts_searched_nodes')}))
    return entities, relationships
