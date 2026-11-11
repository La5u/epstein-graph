"""Generate the offline HTML viewer from viewer/ (WebGL renderer, logic and UI, no dependencies)."""
import hashlib
import base64
import gzip
import json
from collections import Counter, defaultdict
import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import networkx as nx
import node_cleanup
import fastlayout
from viewer_documents import augment
from build_contacts import main as contacts_main

ROOT = Path(__file__).resolve().parents[1]
# viewer.html template plus renderer.js, graph_logic.js and app.js, inlined at build time.
ASSETS = ROOT / 'viewer'
CO_MENTION_TYPES = ('shares_exported_efta_reference', 'co_mentioned_in_documents', 'victim_document_co_mention', 'same_document')
EDGE_COLORS = {'flew_together': '#c084fc', 'co_mentioned_in_documents': '#f59e0b', 'same_day_presence': '#a78bfa', 'same_day_document_mentions': '#a78bfa', 'flew_to_or_from': '#c084fc', 'mentioned_place_in_email': '#e879f9', 'victim_of': '#f87171', 'victim_document_co_mention': '#fca5a5'}
COLORS = {'person': '#ffffff', 'survivor': '#ef4444', 'organization': '#34d399', 'place': '#c084fc', 'vehicle': '#facc15', 'contact': '#2dd4bf', 'participant': '#a3a3a3', 'website': '#60a5fa'}
EDGE_COLORS['same_document'] = '#38bdf8'
# Viewer categories: a few coloured types, each with subtypes (a secondary filter). The graph files keep their own types.
ISLANDS = {'Little St. James Island', 'Great St. James Island'}  # Epstein's islands: properties, filed upstream as locations


def category(e):
    """(viewer type, subtype or None) of a graph entity."""
    kind = e.get('entity_type')
    if kind == 'shell_company':
        return 'organization', 'shell company'
    if kind == 'organization':
        return 'organization', e.get('subtype') or 'other'
    if kind in ('location', 'airport', 'property'):
        if kind == 'property' or e.get('name') in ISLANDS:
            return 'place', 'property'
        if kind == 'airport' or e.get('place_type') == 'airport':
            return 'place', 'airport'
        return 'place', e.get('place_type') or 'other'
    if kind in ('aircraft', 'vehicle'):
        return 'vehicle', e.get('vehicle_type') or ('aircraft' if kind == 'aircraft' else 'other')
    if kind in ('email', 'phone'):
        return 'contact', kind
    return kind, None


def with_category(e):
    kind, sub = category(e)
    out = {**e, 'entity_type': kind, **({'subtype': sub} if sub else {}),
           **({'original_entity_type': e['entity_type']} if kind != e.get('entity_type') and 'original_entity_type' not in e else {})}
    if e.get('expand'):
        out['expand'] = [{**x, 'entity_type': category(x)[0], **({'subtype': category(x)[1]} if category(x)[1] else {})} for x in e['expand']]
    return out


def co_passengers(r):
    """Upstream 'friend' links that its own note says are shared flights ('Co-appeared on N flights together')."""
    m = r.get('metadata')
    try:
        m = json.loads(m) if isinstance(m, str) else m
    except ValueError:
        return r
    if r.get('relationship_type') == 'associated_with' and isinstance(m, dict) and str(m.get('notes', '')).startswith('Co-appeared on'):
        return {**r, 'relationship_type': 'flew_together', 'original_relationship_type': 'associated_with'}
    return r


def layout_positions(entities, relationships, spacing=240, cache=None):
    """Precompute a force-directed layout so the browser only polishes it.

    In-browser stabilization from random positions took ~16 s for ~1k nodes.
    The layout is seeded, so it is cached in output/ keyed on the exact
    node order and weighted edges; the ~1 s spring layout reruns only when
    the graph changes (C Barnes-Hut, scripts/fastlayout.c).
    """
    cache = ROOT / 'output/graph_layout_cache.json' if cache is None else Path(cache)
    pulls = [(r['source_entity_id'], r['target_entity_id'], math.log10(1 + edge_records(r)) + 0.3)
             for r in relationships if r['source_entity_id'] != r['target_entity_id']]
    key = hashlib.sha256(json.dumps([spacing, [e['id'] for e in entities], pulls]).encode()).hexdigest()
    try:
        cached = json.loads(cache.read_text())
        if cached['key'] == key:
            return {node: (x, y) for node, x, y in cached['positions']}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    positions = _layout(entities, pulls, spacing)
    partial = cache.with_suffix('.partial.json')
    try:
        partial.write_text(json.dumps({'key': key, 'positions': [[node, x, y] for node, (x, y) in positions.items()]}))
        partial.replace(cache)
    except OSError:
        pass  # a cache is optional
    return positions


def _layout(entities, pulls, spacing):
    graph = nx.Graph()
    graph.add_nodes_from(e['id'] for e in entities)
    # Pull grows with the records behind a pair, summed over its link types.
    for a, b, pull in pulls:
        graph.add_edge(a, b, weight=graph.edges[a, b]['weight'] + pull if graph.has_edge(a, b) else pull)
    # Lay out the main component alone; spring_layout flings isolates and small
    # components to a far ring, which forces "fit" to zoom the core to a speck.
    components = sorted(nx.connected_components(graph), key=len, reverse=True)
    main = graph.subgraph(components[0]) if components else graph
    k = 3 / math.sqrt(max(len(main), 1))
    # Barnes-Hut in C (~1 s for 10k nodes; networkx's O(n^2) loop took ~6 min); networkx without a compiler.
    positions = fastlayout.spring_layout(main, seed=42, iterations=150, k=k, theta=0.5)
    if positions is None:
        positions = nx.spring_layout(main, seed=42, iterations=150, k=k)
    scale = spacing * math.sqrt(max(len(main), 1)) / 2
    positions = {node: (float(x) * scale, float(y) * scale) for node, (x, y) in positions.items()}
    # Pack the rest in rows below the main component, one component per cell.
    bottom = max((y for _, y in positions.values()), default=0) + spacing * 2
    left = min((x for x, _ in positions.values()), default=0)
    width = max((x for x, _ in positions.values()), default=0) - left or spacing * 10
    cell, x, y = spacing * 0.6, left, bottom
    for component in components[1:]:
        members = sorted(component, key=str)
        for i, node in enumerate(members):
            positions[node] = (x + (i % 3) * cell / 3, y + (i // 3) * cell / 3)
        x += cell * (1 + (len(members) > 3))
        if x > left + width:
            x, y = left, y + cell * 1.5
    return {node: (round(x, 1), round(y, 1)) for node, (x, y) in positions.items()}


def edge_title(r):
    kind, weight = r['relationship_type'], r['weight']
    if kind in ('sent_to', 'cc_to', 'header_label_association', 'upstream_resolved_header_identity'):
        return f'{kind} · {weight} source message records'
    if kind in ('co_mentioned_in_documents', 'same_document'):
        return f'{kind} · {weight} shared documents (reading lead, not a relationship)'
    if kind in ('same_day_presence', 'same_day_document_mentions'):
        return f'same-day dated mentions · {weight} shared dates in flight logs / schedules (not verified presence or proof they met)'
    if kind == 'shares_exported_efta_reference':
        return f'{kind} · {weight} shared EFTA references'
    if kind == 'flew_together':
        return f'flew together · {weight} shared flights in the flight logs (upstream "friend" link; not evidence of a relationship)'
    if kind == 'victim_of':
        return f'survivor_of · upstream survivor-perpetrator allegation link ({weight} record; abuse type and dates in the record). An allegation recorded upstream, not a proven offence; survivors carry a curated label, not a corpus-derived identity.'
    if kind == 'victim_document_co_mention':
        m = r.get('metadata') if isinstance(r.get('metadata'), dict) else {}
        docs = m.get('shared_documents', weight)
        score = m.get('specificity_score', weight)
        return (f'survivor_document_co_mention · {docs} shared EFTA documents with a censored survivor designator, specificity {score} '
                '(each document counts 1/(names in it - 1), so long compilations naming many people add little). '
                'A reading lead, not a relationship; no survivor identity is resolved.')
    return f'{kind} · upstream weight {weight} (type-specific meaning, not confidence)'


def render(template, values):
    """Single-pass placeholder substitution: inserted text (upstream names, vis
    assets) is never rescanned, so a name containing a placeholder can't break it."""
    return re.sub('|'.join(sorted(map(re.escape, values), key=len, reverse=True)), lambda m: values[m.group()], template)


MAX_MERGED, MAX_LIST, MAX_REFERENCES = 100, 10, 3
MAX_REVIEW = 30  # reattributed / kept-apart entries listed per node


def with_subpoenas(entities, path=ROOT / 'output/document_info.db'):
    """Entities named as the target of a DOJ subpoena (build_doc_info.py, from the upstream
    prosecutorial_query_graph) get record['subpoenas'] = [{target, date, category, efta}].
    Same name after dropping legal forms and punctuation ("Deutsche Bank AG" = node
    "Deutsche Bank"); persons and organizations only, never survivors; a target listing
    several entities ("A / B") matches each part."""
    import sqlite3
    from contextlib import closing
    from build_companies import base_of, normal_name
    if not Path(path).exists():
        return entities
    def key(name):  # 'Dfoken K. Indyke' = 'Darren Indyke', 'JP Morgan Chase' = 'JPMorgan Chase'
        name = re.sub(r'^the\s+|\s+and attendant accounts$', '', name.split(',')[0].strip(), flags=re.I)
        words = [w for w in base_of(normal_name(name)).split() if len(w) > 1 or w in ('&',)]
        return ''.join(words)
    by_name = defaultdict(list)
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        for efta, target, category, date in db.execute('SELECT efta, target, category, date_issued FROM subpoenas ORDER BY date_issued, efta'):
            for part in re.split(r'\s+/\s+|;', target):
                part = SUBPOENA_ALIASES.get(part.strip(), part)
                if len(k := key(part)) >= 4:
                    by_name[k].append({'target': target, 'date': date or None, 'category': category, 'efta_number': efta})
    out = []
    for e in entities:
        # An "Individual" subpoena names a person (not their PLLC); institution categories name organizations.
        person = e['entity_type'] == 'person'
        found = [x for x in by_name.get(key(e['name']), ()) if (x['category'] in PERSON_CATEGORIES) == person] \
            if e['entity_type'] in ('person', 'organization', 'shell_company') else None
        out.append({**e, 'subpoenas': found} if found else e)
    return out


SUBPOENA_ALIASES = {'JPMC': 'JPMorgan Chase', 'JP Morgan Chase': 'JPMorgan Chase'}
PERSON_CATEGORIES = {'Individual', 'Redacted'}


def document_lines(nodes, edges, path=ROOT / 'output/document_info.db'):
    """{efta: short line} (build_doc_info.py: type, subpoena, FBI case, DOJ version
    status, published reports) for the documents the viewer lists."""
    import sqlite3
    from contextlib import closing
    wanted = set()
    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get('efta_number'), str):
                wanted.add(value['efta_number'])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for item in nodes + edges:
        walk(item['record'])
    if not Path(path).exists() or not wanted:
        return {}
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        db.execute('CREATE TEMP TABLE wanted(efta TEXT PRIMARY KEY)')
        db.executemany('INSERT OR IGNORE INTO wanted VALUES (?)', [(e,) for e in wanted])
        return dict(db.execute('SELECT i.efta, i.line FROM info i JOIN wanted w ON w.efta = i.efta WHERE i.line IS NOT NULL'))


def shared_tables(nodes, edges):
    """Pull repeated strings out of the records: document URLs become one
    {efta: url} table (records keep the EFTA number) and edge meanings one list
    (records keep meaning_ref). The viewer restores both when showing a record."""
    urls, meanings, index = {}, [], {}
    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get('efta_number'), str) and isinstance(value.get('pdf_url'), str):
                urls[value['efta_number']] = value.pop('pdf_url')
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for item in nodes + edges:
        item['record'] = json.loads(json.dumps(item['record']))  # private copy; source records stay intact
        walk(item['record'])
    for edge in edges:
        metadata = edge['record'].get('metadata')
        if isinstance(metadata, dict) and isinstance(metadata.get('meaning'), str):
            text = metadata.pop('meaning')
            metadata['meaning_ref'] = index.setdefault(text, len(meanings))
            if index[text] == len(meanings):
                meanings.append(text)
    # Most URLs are prefix + EFTA + suffix: store each pattern once, then an index per document.
    templates, compact = [], {}
    for efta, url in urls.items():
        at = url.find(efta)
        pattern = (url[:at], url[at + len(efta):]) if at >= 0 else (url, None)
        if pattern not in templates:
            templates.append(pattern)
        compact[efta] = templates.index(pattern) if len(templates) < 64 else url
    return {'templates': templates, 'documents': compact}, meanings


DETAIL_CHUNK_SIZE = 256
NODE_HOT_FIELDS = ('id', 'name', 'entity_type', 'relevance', 'label_quality', 'suspected_parse_artifact',
                   'legal_status_flags', 'dual_status', 'expand_total', 'subtype', 'document_isolation_reason', 'websites')


def gzip_json(value):
    """Non-executable base64: safe inside an HTML script data block, including upstream text."""
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    return base64.b64encode(gzip.compress(raw, mtime=0)).decode('ascii')


def lazy_payload(nodes, edges, urls, meanings, docinfo):
    """Only hot fields survive startup. Full records live in independently compressed chunks.

    Resolve document tables inside each chunk so opening one panel doesn't need to
    inflate a global URL/info index. Repeated URLs compress well within a chunk.
    Nothing is removed from the existing slim records.
    """
    chunks, batch = [], []

    def documents(value):
        if isinstance(value, dict):
            efta = value.get('efta_number')
            if isinstance(efta, str):
                entry = urls['documents'].get(efta)
                if isinstance(entry, int):
                    prefix, suffix = urls['templates'][entry]
                    value['pdf_url'] = prefix if suffix is None else prefix + efta + suffix
                elif isinstance(entry, str):
                    value['pdf_url'] = entry
                if efta in docinfo:
                    value['document_info'] = docinfo[efta]
            for item in value.values():
                documents(item)
        elif isinstance(value, list):
            for item in value:
                documents(item)

    for index, item in enumerate(nodes + edges):
        record = item['record']
        documents(record)
        batch.append(record)
        item['detail'] = index
        if index < len(nodes):
            hot = {key: record[key] for key in NODE_HOT_FIELDS if key in record}
        else:
            hot = {key: record[key] for key in ('id', 'relationship_type', 'weight') if key in record}
            metadata = record.get('metadata') or {}
            hot['metadata'] = {key: metadata[key] for key in ('new_link', 'shared_documents', 'specificity_score') if key in metadata}
            # Reconstructed once in the browser from type/weight and the style table.
            for key in ('title', 'color', 'dashed', 'arrow'):
                item.pop(key, None)
        if isinstance(record.get('documents'), dict):
            hot['documents'] = {key: record['documents'][key] for key in ('n', 'records_n') if key in record['documents']}
        item['record'] = hot
        if len(batch) == DETAIL_CHUNK_SIZE:
            chunks.append(gzip_json(batch))
            batch = []
    if batch:
        chunks.append(gzip_json(batch))
    blocks = '\n'.join(f'<script type="application/octet-stream" id="detail-{i}">{chunk}</script>'
                       for i, chunk in enumerate(chunks))
    payload = {'nodes': nodes, 'edges': edges, 'meanings': meanings, 'detailChunkSize': DETAIL_CHUNK_SIZE}
    return gzip_json(payload), blocks


MAX_EXPAND = 25  # hidden neighbours per node the viewer can pull in ("Expand")


def with_expansion(entities, db_path=ROOT / 'output/communication_graph.db', limit=MAX_EXPAND):
    """Attach each node's strongest communication partners that are not in the view
    (expand: [{id, entity_type, name, relationship_type, records, date_first, date_last,
    outgoing}], expand_total), so the viewer can pull them in on demand without raising
    the global caps. One hop, from the complete pair table; names stay pseudonyms (real
    ones come with the unlock). A server (SQLite / Neo4j) could serve the same shape
    for larger graphs."""
    if not db_path.exists():
        return entities
    comm = lambda i: f'kg:{i}' if isinstance(i, int) else str(i)
    view = lambda i: int(i[3:]) if i.startswith('kg:') else i
    in_view = {comm(e['id']) for e in entities}
    candidates = {}
    with closing(sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)) as db:
        for a, b, kind, count, first, last in db.execute(
                "SELECT source_id, target_id, relationship_type, record_count, date_first, date_last FROM edges WHERE relationship_type IN ('sent_to', 'cc_to')"):
            if a == b:
                continue
            for here, there, outgoing in ((a, b, True), (b, a, False)):
                if here in in_view and there not in in_view:
                    candidates.setdefault(here, []).append((count, there, kind, first, last, outgoing))
        wanted = {c[1] for items in candidates.values() for c in items}
        stubs = {i: (kind, label) for i, kind, label in db.execute('SELECT id, entity_type, label FROM nodes') if i in wanted}
        classes = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='node_classes'").fetchone():
            classes = {i: (kind, quality) for i, kind, quality in db.execute('SELECT id, entity_type, label_quality FROM node_classes') if i in wanted}
    normalized = ROOT / 'output/normalized_entities.json'
    names = {f"kg:{e['id']}": (e['entity_type'], e['name']) for e in json.loads(normalized.read_text())} if normalized.exists() else {}
    review = json.loads((ROOT / 'config/node_review.json').read_text()).get('participants', {}) if (ROOT / 'config/node_review.json').exists() else {}
    # Survivors render as their own type in the view (build_contacts.apply_victim_type); expanded ones too.
    from victim_policy import victim_ids
    survivors = victim_ids([{'id': i, 'name': n, 'entity_type': k} for i, (k, n) in names.items()]
                           + [{'id': i, 'name': '', 'entity_type': k} for i, (k, _) in stubs.items()],
                           config=ROOT / 'config/victims.json', survivor_config=ROOT / 'config/survivors.json',
                           registry=ROOT / 'data/raw/persons_registry.json')
    out = []
    for e in entities:
        items = sorted(candidates.get(comm(e['id']), []), key=lambda c: (-c[0], str(c[1])))
        if not items:
            out.append(e)
            continue
        listed = []
        for count, there, kind, first, last, outgoing in items[:limit]:
            entity_type, name = names.get(there) or stubs.get(there, ('participant', there))
            quality = 'ok'
            if there in classes:
                entity_type, quality = classes[there]  # same type and label quality as in-view header nodes
            elif review.get(there, {}).get('verdict') == 'ok_entity':
                entity_type = 'organization'
            if there in survivors:
                entity_type, quality = 'survivor', 'ok'
            listed.append({'id': view(there), 'entity_type': entity_type, 'name': name, 'relationship_type': kind, 'records': count,
                           'date_first': first, 'date_last': last, 'outgoing': outgoing,
                           **({'label_quality': quality} if quality not in ('ok', 'ok_person', 'ok_entity') else {})})
        out.append({**e, 'expand': listed, 'expand_total': len({c[1] for c in items})})
    return out


def with_identity_review(entities, path=ROOT / 'output/identity_review.json'):
    """Attach what build_communications merged into, moved into / out of and kept apart
    from each node (pseudonymous IDs; real names come with the unlock), so false merges
    can be spotted from the node's detail panel."""
    from corpus import document_urls
    review = json.loads(path.read_text()) if path.exists() else {}
    urls = document_urls({efta for item in review.values() for k in ('reattributed_in', 'reattributed_out')
                          for x in item.get(k, [])[:MAX_REVIEW] for efta in x.get('documents', [])})
    for item in review.values():
        for k in ('reattributed_in', 'reattributed_out'):
            for x in item.get(k, []):
                x['documents'] = [{'efta_number': e, **({'pdf_url': urls[e]} if e in urls else {})} for e in x.get('documents', [])]
    out = []
    for e in entities:
        item = review.get(str(e['id']))
        if not item:
            out.append(e)
            continue
        counts = item.get('merged_records', {})
        merged = sorted(({**m, 'records': counts.get(str(m.get('id')), 0)} for m in e.get('merged_identities', [])),
                        key=lambda m: (-m['records'], str(m.get('id'))))
        extra = {k: item[k][:MAX_REVIEW] for k in ('reattributed_in', 'reattributed_out', 'kept_apart') if item.get(k)}
        out.append({**e, **({'merged_identities': merged} if merged else {}), **({'identity_review': extra} if extra else {})})
    return out


def short_pseudonyms(e):
    """Header-name and street-address pseudonyms are shown as their hash alone ('HeaderLabel_<hash>' -> '<hash>')."""
    strip = lambda name: next((name[len(p):] for p in ('HeaderLabel_', 'Address_') if isinstance(name, str) and name.startswith(p)), name)
    out = {**e, 'name': strip(e['name'])}
    if e.get('expand'):
        out['expand'] = [{**x, 'name': strip(x.get('name'))} for x in e['expand']]
    return out


PSEUDONYM = re.compile(r'^(?:[A-Za-z]+_)?[0-9a-f]{20,}$')


def hide_merged_spellings(e):
    """A node shown under a pseudonym must not carry the real spellings merged
    into it ('value': 'Mark Zeff' merged into participant 84808c...)."""
    merged = e.get('merged_identities')
    if not (isinstance(merged, list) and PSEUDONYM.match(str(e.get('name', '')))):
        return e
    return {**e, 'merged_identities': [{k: v for k, v in m.items() if k != 'value'} if isinstance(m, dict) else m
                                       for m in merged]}


ACCUSATION_ROLES = {'perpetrator', 'enabler', 'co-conspirator', 'coconspirator', 'accomplice', 'abuser', 'recruiter'}


def without_accusation(metadata):
    """Metadata (JSON text or dict) minus an accusatory person_type; unchanged when it has none."""
    parsed = json.loads(metadata) if isinstance(metadata, str) and metadata.startswith('{') else metadata
    if not isinstance(parsed, dict) or str(parsed.get('person_type', '')).casefold() not in ACCUSATION_ROLES:
        return metadata
    parsed = {k: v for k, v in parsed.items() if k != 'person_type'}
    return json.dumps(parsed) if isinstance(metadata, str) else parsed


def hide_unconvicted_accusation(e):
    """Upstream role labels such as 'enabler' are an accusation; the viewer shows
    one only next to a reported conviction. Merged source records carry the same
    label. The full graph keeps it."""
    if 'convicted' in (e.get('legal_status_flags') or []):
        return e
    slim = {**e, 'metadata': without_accusation(e.get('metadata'))} if 'metadata' in e else dict(e)
    if isinstance(e.get('original_records'), list):
        slim['original_records'] = [{**r, 'metadata': without_accusation(r.get('metadata'))} if isinstance(r, dict) and 'metadata' in r else r
                                    for r in e['original_records']]
    return slim


def slim_node(e):
    """Viewer copy of a node: merged identities summarized (counts per rule) with
    the MAX_MERGED bringing the most records listed. Full lists stay in
    graph_entities.json / identity_map.json and the private document pages."""
    e = hide_unconvicted_accusation(hide_merged_spellings(e))
    merged = e.get('merged_identities')
    if not isinstance(merged, list) or len(merged) <= MAX_MERGED:
        return e
    ordered = sorted(merged, key=lambda m: (-m.get('records', 0), str(m.get('value', m.get('id')))))  # biggest merges first
    return {**e, 'merged_identities': ordered[:MAX_MERGED], 'merged_identity_count': len(merged),
            'merged_identities_by_rule': dict(Counter(str(m.get('rule', '')).split(':')[0] for m in merged).most_common())}


def slim_edge(r):
    """Viewer copy of an edge with long id lists cut to MAX_LIST (+ total count)."""
    metadata = r.get('metadata')
    if not isinstance(metadata, dict):
        return r
    slim = dict(metadata)
    for key, value in metadata.items():
        limit = MAX_REFERENCES if key == 'sample_source_references' else MAX_LIST
        if isinstance(value, list) and len(value) > limit:
            slim[key], slim[key + '_total'] = value[:limit], len(value)
    return {**r, 'metadata': slim}


def edge_records(r):
    """Records behind an edge: messages, shared documents or flights (>= 1)."""
    metadata = r.get('metadata') if isinstance(r.get('metadata'), dict) else {}
    try:
        return max(float(metadata.get('record_count') or r.get('weight') or 1), 1.0)
    except (TypeError, ValueError):
        return 1.0


def edge_width(r):
    """1-8 px, logarithmic in the records behind an edge."""
    return round(min(8.0, 1 + 1.75 * math.log10(edge_records(r))), 2)


def edge_pull(r):
    """Spring length factor for live physics: 1 for a single record, down to 0.35."""
    return round(max(0.35, 1 / (1 + 0.45 * math.log10(edge_records(r)))), 3)


MAX_EDGE_DOCS, MAX_NODE_DOCS, MAX_RECORDS = 20, 10, 10
HOW_ORDER = ('cited in the record', 'email header', 'designator and name in one document', 'email mentions the place',
             'names both', 'full text names both (fallback)')


def document_index(path=ROOT / 'output/documents.db', node_ids=None, edge_ids=None):
    """({edge id: documents}, {node id: documents}) from build_documents.py, each
    {'n': total, 'docs': first few, 'records': [...], 'records_n': total}. Most
    direct evidence first (cited, header) and full-text fallbacks last."""
    import sqlite3
    from contextlib import closing
    from corpus import document_urls
    if not Path(path).exists():
        return {}, {}
    rank = {how: i for i, how in enumerate(HOW_ORDER)}
    edges, nodes = {}, {}
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        # The full index includes every retained NER node. Read only the
        # viewer's IDs before materializing document lists on a 15 GB machine.
        for kind, identities, tables, column in (
                ('node', node_ids, ('node_documents','node_records'), 'node_id'),
                ('edge', edge_ids, ('edge_documents','edge_records'), 'edge_id')):
            if identities is None: continue
            wanted = 'viewer_' + kind + '_ids'
            db.execute('CREATE TEMP TABLE '+wanted+'(id TEXT PRIMARY KEY)')
            db.executemany('INSERT INTO '+wanted+' VALUES (?)',((str(i),) for i in sorted(set(identities),key=str)))
            for table in tables:
                db.execute('CREATE TEMP VIEW '+table+' AS SELECT rowid AS rowid,* FROM main.'+table
                           +' WHERE '+column+' IN (SELECT id FROM '+wanted+')')
        for edge, efta, page, how in db.execute('SELECT edge_id, efta, page, how FROM edge_documents'):
            edges.setdefault(edge, {'n': 0, 'docs': []})
            edges[edge]['n'] += 1
            edges[edge]['docs'].append((rank.get(how, 9), efta, page, how))
        for edge, kind, ref, url in db.execute('SELECT edge_id, kind, ref, url FROM edge_records ORDER BY rowid'):
            item = edges.setdefault(edge, {'n': 0, 'docs': []})
            item.setdefault('records', []).append({'kind': kind, 'ref': ref, 'url': url})
        for node, n in db.execute('SELECT node_id, count(DISTINCT efta) FROM node_documents GROUP BY 1'):
            nodes[node] = {'n': n, 'docs': []}
        for node, efta, page in db.execute('SELECT node_id, efta, page FROM node_documents WHERE edge_id IS NULL ORDER BY node_id, efta'):
            nodes[node]['docs'].append((0, efta, page, 'names this node'))
        for node, kind, ref, url in db.execute('SELECT node_id, kind, ref, url FROM node_records'):
            nodes.setdefault(node, {'n': 0, 'docs': []}).setdefault('records', []).append({'kind': kind, 'ref': ref, 'url': url})
    for items, limit in ((edges, MAX_EDGE_DOCS), (nodes, MAX_NODE_DOCS)):
        for item in items.values():
            item['docs'] = sorted(item['docs'])[:limit]
            records = item.pop('records', [])
            if records:
                item['records'], item['records_n'] = records[:MAX_RECORDS], len(records)
    urls = document_urls({d[1] for items in (edges, nodes) for item in items.values() for d in item['docs']})
    for items in (edges, nodes):
        for item in items.values():
            item['docs'] = [{'efta_number': efta, **({'pdf_url': urls[efta]} if efta in urls else {}),
                             **({'page': page} if page is not None else {}), 'how': how}
                            for _, efta, page, how in item['docs']]
    return edges, nodes


ENTITY_LAYER_SOURCE = 'Full-text entity mentions; NER is unverified'  # build_entities.py


def viewer_slice(entities, relationships):
    """Embed entity/company-layer nodes only with an exported link.

    The incidental switch is a visibility filter, not an embedding decision.
    Unlinked extracted nodes remain in the full JSON/SQLite/Neo4j graph.
    """
    linked = {x for r in relationships for x in (r['source_entity_id'], r['target_entity_id'])}
    return [e for e in entities if (e.get('source') not in (ENTITY_LAYER_SOURCE, 'full-text legal name (scripts/extract_companies.py)')
                                  and not str(e['id']).startswith('company_')) or e['id'] in linked]


def fold_own_websites(entities, relationships):
    """A website whose only viewer partner is its organization becomes a line on it."""
    entities, relationships, fold = node_cleanup.fold_own_websites(entities, relationships)
    if fold:
        print(f'Viewer: {len(fold)} websites shown on their organization instead of as separate dots', flush=True)
    return entities, relationships


def drop_late_isolates(entities, relationships):
    """Label rules run after the same-document fallback and can hide a node's
    only partners. Entity-layer nodes left without a visible partner in every
    view mode leave the viewer slice (the full graph keeps them); any other
    unconnected node gets its reason."""
    from viewer_documents import isolates
    modes = ('default', 'unlocked', 'all_types')
    alone = {m: isolates(entities, relationships, m) for m in modes}
    everywhere = set.intersection(*alone.values())
    drop = {i for i in everywhere if str(i).startswith(('entity_', 'company_'))}
    for e in entities:
        if e['id'] in set().union(*alone.values()) - drop and not e.get('document_isolation_reason'):
            e['document_isolation_reason'] = 'document partners are hidden by this view'
    if drop:
        print(f'Viewer: {len(drop)} entity-layer nodes left without visible partners by label rules are not embedded', flush=True)
    return ([e for e in entities if e['id'] not in drop],
            [r for r in relationships if r['source_entity_id'] not in drop and r['target_entity_id'] not in drop])


def main(path=ROOT / 'output/graph.html', rebuild=True, secret=True):
    """Write the viewer. Real header names are embedded only encrypted
    (scripts/viewer_secret.py) and only when secret=True and a password is set;
    contacts follow the GDPR publication policy; other values use HMAC labels."""
    from viewer_label_payload import label_payload, fold_viewer_resolutions
    from airports import Airports
    path = Path(path)
    if rebuild:
        contacts_main()
    relationships = [co_passengers(r) for r in json.loads((ROOT / 'output/graph_relationships.json').read_text())]
    entities = viewer_slice(json.loads((ROOT / 'output/graph_entities.json').read_text()), relationships)
    from contact_resolution import viewer_folds
    entities, relationships = viewer_folds(entities, relationships, ROOT / 'output')
    from viewer_label_payload import include_reviewed_targets
    entities = include_reviewed_targets(entities, ROOT / 'output/graph_entities.json')
    entities = with_expansion(with_identity_review(entities, ROOT / 'output/identity_review.json'), ROOT / 'output/communication_graph.db')
    entities = with_subpoenas(entities, ROOT / 'output/document_info.db')
    entities = [with_category(short_pseudonyms(e)) for e in entities]
    badges = {'convicted': 'CONV', 'in_custody': 'CUST', 'under_investigation': 'INV', 'deceased': 'DEAD'}
    edge_documents, node_documents = document_index(ROOT / 'output/documents.db',
        node_ids={e['id'] for e in entities}, edge_ids={r['id'] for r in relationships})
    entities = [{**e, 'documents': node_documents[str(e['id'])]} if str(e['id']) in node_documents else e for e in entities]
    relationships = [{**r, 'documents': edge_documents[str(r['id'])]} if str(r['id']) in edge_documents else r
                     for r in relationships]
    entities, relationships = augment(entities, relationships, out=ROOT / 'output')
    from viewer_label_payload import early_retypes
    entities = early_retypes(entities)
    import survivor_privacy
    entities, relationships, protected, scrub, private_contacts = survivor_privacy.protect(entities, relationships, survivor_privacy.load_key())
    import public_names
    verdicts = public_names.load_verdicts()
    entities, hidden_people, scrub_people = public_names.apply(entities, verdicts, survivor_privacy.load_key(), survivor_privacy.scrubber)
    protected = {**protected, **hidden_people}
    # Other layers can contain incidental contacts inside company names or
    # metadata. Preserve those fields in SECRET before the public scrub.
    from contact_publication import text_scrubber
    incidental_scrub = text_scrubber({})
    for entity in entities:
        clean = incidental_scrub(entity)
        if clean != entity:
            identity = str(entity['id'])
            entry = protected.setdefault(identity, {})
            if clean.get('name') != entity['name']:
                entry.setdefault('value', entity['name'])
            fields = {k:v for k,v in entity.items() if k not in {'id','name','entity_type'} and clean.get(k) != v}
            if fields:
                entry['fields'] = {**entry.get('fields', {}), **fields}
    labels = {}
    resolutions = {}
    gazetteer_path = ROOT / 'config/entity_gazetteer.json'
    gazetteer = json.loads(gazetteer_path.read_text()) if gazetteer_path.exists() else {}
    contact_kinds = {}
    entities, encrypted, contacts = label_payload(entities, path, secret=secret, resolutions=resolutions,
                                                 airports=Airports(), gazetteer=gazetteer, reference_out=ROOT / 'output', protected=protected, labels_out=labels, contact_kinds_out=contact_kinds)
    from contact_publication import publish, named_public_ids, text_scrubber
    allowed = survivor_privacy.public_contacts(contacts, scrub, private_contacts, survivor_privacy.survivor_mailbox_forms())
    contact_denied = set(contacts) - set(allowed)
    public_people = named_public_ids(entities, labels, verdicts)
    # Numbered references ("Jet Blue #65", "Bill 7", "Bell 407"): config/node_vocabulary.json + scripts/node_cleanup.py.
    entities = node_cleanup.numbered_families(entities, node_cleanup.load_vocabulary(), resolutions)
    entities, relationships = fold_viewer_resolutions(entities, relationships, resolutions)
    # Categories (place subtypes the viewer hides) first: isolation is judged on final types.
    entities = [with_category(e) for e in entities]
    entities, relationships = fold_own_websites(entities, relationships)
    entities, relationships = drop_late_isolates(entities, relationships)
    positions = layout_positions(entities, relationships)
    nodes = [dict(id=e['id'], label=e['name'] + ((' [' + ' / '.join(badges[s] for s in e.get('legal_status_flags', [])) + ']') if e.get('legal_status_flags') else ''),
                  x=positions[e['id']][0], y=positions[e['id']][1], record=slim_node(e)) for e in entities]
    edges = [dict(id=r['id'], **{'from': r['source_entity_id'], 'to': r['target_entity_id']},
                  color=EDGE_COLORS.get(r['relationship_type'], '#64748b'), width=edge_width(r), pull=edge_pull(r),
                  dashed=r['relationship_type'] in CO_MENTION_TYPES or r['relationship_type'] in ('same_day_presence', 'same_day_document_mentions'),
                  arrow=r['relationship_type'] in ('sent_to', 'cc_to'),
                  title=edge_title(r), record=slim_edge(r)) for r in relationships]
    # Public contacts must be reachable in the exported panels. The encrypted
    # reveal above still retains the complete untruncated merge membership.
    from viewer_secret import viewer_ids
    wanted_contacts = set(map(str, viewer_ids([n['record'] for n in nodes])))
    contacts, contact_counts = publish(entities, {i:v for i,v in contacts.items() if i in wanted_contacts},
                                      named_ids=public_people, denied=contact_denied, out=ROOT / 'output', kinds=contact_kinds)
    contact_scrub = text_scrubber(contacts)
    contacts.update({i:contact_scrub(name) for i,name in public_names.shown_names(entities, labels, verdicts).items()})
    (ROOT / 'output/public_contact_summary.json').write_text(json.dumps(contact_counts, indent=2) + '\n')
    nodes, edges = contact_scrub(scrub_people(scrub(nodes))), contact_scrub(scrub_people(scrub(edges)))  # labels and records added since
    # Escape script delimiters in untrusted upstream strings. Detail panels use textContent.
    urls, meanings = shared_tables(nodes, edges)
    docinfo = contact_scrub(scrub_people(scrub(document_lines(nodes, edges))))  # "subpoena to X" lines are added after the scrub above
    payload, detail_chunks = lazy_payload(nodes, edges, urls, meanings, docinfo)
    summary_path = ROOT / 'output/communication_summary.json'
    coverage = 'Communication metadata not yet built.'
    if summary_path.exists():
        stats = json.loads(summary_path.read_text())
        coverage = (f'Metadata: {stats["non_noise_message_records"]:,} non-noise message records processed; '
                    f'{stats["directed_pair_edges"]:,} full-graph pair edges. '
                    f'Viewer shows up to {stats["view_pair_limit"]:,} highest-record-count sender/recipient pairs plus associated identity links—not the full network.')
    status_counts = {status: sum(status in e.get('legal_status_flags', []) for e in entities) for status in badges}
    status_coverage = (f'Loaded: {status_counts["convicted"]} reported conviction markers, '
                       f'{status_counts["in_custody"]} custody markers, '
                       f'{status_counts["under_investigation"]} investigation markers, '
                       f'{status_counts["deceased"]} deceased markers. '
                       'Upstream status dates are unspecified; current custody and investigation status have not been verified.')
    comention_path = ROOT / 'output/comention_summary.json'
    if comention_path.exists():
        c = json.loads(comention_path.read_text())
        coverage += (f' Full-text co-mentions: {c["entities_with_hits"]} entities searched across {c["documents_indexed"]:,} documents; '
                     f'{c["pairs_min_shared"]:,} pairs share ≥{c["min_shared_documents"]} documents ({c["pairs_new"]:,} not otherwise linked; see new_connections.csv). '
                     f'Viewer shows the top {c["viewer_edges"]:,} as dashed amber lines.')
    html = render((ASSETS / 'viewer.html').read_text(), {
        'LEGAL_STATUS_COVERAGE': status_coverage, 'COMMUNICATION_COVERAGE': coverage,
        'ENTITY_COUNT': f'{len(entities):,}', 'EDGE_COUNT': f'{len(relationships):,}',
        'RENDERER_JS': (ASSETS / 'renderer.js').read_text(), 'LOGIC_JS': (ASSETS / 'graph_logic.js').read_text(),
        'APP_JS': (ASSETS / 'app.js').read_text(), 'PAYLOAD': payload, 'DETAIL_CHUNKS': detail_chunks, 'COLORS_JSON': json.dumps(COLORS),
        'SECRET_JSON': json.dumps(encrypted),
        'PUBLIC_REVEAL_JSON': json.dumps(contacts, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')})
    path.write_text(html)
    print(path, json.dumps(contact_counts), '(password unlock enabled)' if encrypted else '(public view only)')


def cli(argv=None, path=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    # analyze_graph.py has already written the full graph in build.py.
    kwargs = {'path': path} if path is not None else {}
    main(rebuild='--no-rebuild' not in argv, secret='--public' not in argv, **kwargs)


if __name__ == '__main__':
    cli()
