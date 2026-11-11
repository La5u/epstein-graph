"""Raw heuristic document-evidence ranking and dated timeline (no victim identities).

Statement/testimony are document classifications, lexical victim context is a
same-page FTS proximity match, and privacy is a document-level FOIA-code match.
These categories do not establish a person's role, victim contact or culpability.
Legacy CSV labels (including 'victim_evidence_documents') describe this raw
heuristic union, not verified evidence about a person. SQLite document_reasons
preserves every entity_id/efta/reason match, including privacy-only matches;
sample_evidence_docs remains a truncated convenience field.

    .venv/bin/python scripts/involvement.py

Outputs:
  output/involvement_ranking.csv   per named person: how often they appear in
      victim-statement / sworn-testimony documents, near victim-context words,
      and in documents carrying privacy-redaction codes, vs. what their overall
      document count would predict.
  output/presence_timeline.csv     dated events per person: flights (flight-log
      dates) and schedule mentions ("Appt w/", lunch, dinner, meeting) in dated
      calendar/email documents. Document dates are not appointment dates or
      proof of physical presence; sampled flight metadata is incomplete.
  output/copresence_edges.json     viewer edges between people present on the
      same document dates, NOT physical presence (Jeffrey Epstein excluded).
  output/involvement.db            all of the above for SQL follow-up.

Uses only document types, redaction COUNTS/codes and visible names. Redacted
text is never read or recovered. Survivors (config/survivors.json, upstream
victim labels and victim_of sources) are excluded from every output.
Appearing in these documents is NOT evidence of wrongdoing: lawyers,
investigators and witnesses are named in the same documents. Read the sources.
"""
import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import corpus
from corpus_index import page_map
from survivor_policy import survivor_ids

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'
STATEMENT_TYPES = {'fbi_302', 'fbi_report', 'fbi.report', 'fbi_ec', 'police_report', 'grand_jury'}
TESTIMONY_TYPES = {'deposition', 'transcript.deposition'}
SCHEDULE_TYPES = {'calendar', 'email.header', 'email', 'travel.itinerary'}
VICTIM_TERMS = ('victim', 'victims', 'minor', 'minors', 'underage', 'massage', 'massages', 'recruit', 'recruited')
SCHEDULE_TERMS = ('appt', 'appointment', 'lunch', 'dinner', 'breakfast', 'meeting', 'meet')
PRIVACY_CODES = ('"b 6"', '"b 7 c"')  # FOIA exemption labels printed on redaction boxes
EPSTEIN = 1
VICTIM_WINDOW = 25
SCHEDULE_WINDOW = 6
FTS_WORKERS = 4
RANKING_COLUMNS = (
    'entity_id', 'name', 'role_hint', 'professional_role_likely', 'documents',
    'victim_evidence_documents', 'statement_docs_fbi_police_grand_jury',
    'statement_enrichment', 'sworn_testimony_docs', 'testimony_enrichment',
    'victim_context_docs', 'privacy_coded_docs', 'privacy_enrichment',
    'classified_documents', 'documents_with_redaction_counts',
    'mean_redactions_per_doc', 'redaction_ratio_vs_corpus', 'sample_evidence_docs', 'rank',
)
INTERPRETATION_WARNING = (
    'Raw heuristic document evidence only, never culpability or verified victim contact. '
    'Legacy victim_evidence_documents counts the union of statement, testimony and '
    'lexical victim-context documents; privacy is separate. Document classifications '
    'do not establish that the named person gave a statement or sworn testimony. '
    'Attorneys, investigators and witnesses appear in the same documents.'
)
# Lawyers, prosecutors, investigators and journalists are named in victim evidence
# because of their jobs; flagged so they can be filtered, not removed.
PROFESSIONAL = re.compile(r'\b(legal|attorney|lawyer|prosecutor|judge|counsel|investigator|detective|agent|journalist|reporter|author)\b', re.I)


def metadata(entity):
    value = entity.get('metadata')
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def survivors(entities, relationships):
    """IDs never ranked or placed on a timeline."""
    return survivor_ids(entities, relationships)


def enrichment(hits, observed, population_hits, population):
    if not observed or not population or not population_hits:
        return None
    return round(hits / observed / (population_hits / population), 2)


def corpus_redaction_mean(redactions, universe):
    baseline = [v for d, v in redactions.items() if d in universe and v is not None]
    return sum(baseline) / len(baseline) if baseline else None


def redaction_stats(docs, redactions, universe, corpus_mean=None):
    """Pass corpus_mean (corpus_redaction_mean) when scoring many people; it is
    the same for everyone and costs a full pass over the corpus."""
    known = [redactions[d] for d in docs if redactions.get(d) is not None]
    mean = sum(known) / len(known) if known else None
    if corpus_mean is None:
        corpus_mean = corpus_redaction_mean(redactions, universe)
    return (len(known), round(mean, 1) if mean is not None else None,
            round(mean / corpus_mean, 2) if mean is not None and corpus_mean else None)


def iso_date(value):
    """True for a real YYYY-MM-DD calendar date."""
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return False
    try:
        datetime.fromisoformat(value)  # C parser; strptime is ~20x slower over 1M dates
    except ValueError:
        return False
    return True


def flight_evidence(relationship):
    """Preserve date-specific flight records, distinguish sampled aggregate dates."""
    m = metadata(relationship)
    covered = set()
    records = m.get('flights') or []
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict) and iso_date(record.get('date')):
            covered.add(record['date'])
            yield record['date'], json.dumps({'relationship_id': relationship['id'], 'flight': record}, sort_keys=True)
    for date in m.get('sample_dates') or []:
        if date not in covered and iso_date(date):
            yield date, json.dumps({'relationship_id': relationship['id'], 'sample_date': date,
                                   'provenance': 'aggregate metadata; flight identity unavailable'}, sort_keys=True)


def fts_phrase(text):
    return '"' + ' '.join(re.findall(r'\w+', text)) + '"'


def input_stamp(path):
    """Reference an input by path, size and mtime, never by its contents."""
    path = Path(path).resolve()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {'path': str(path), 'exists': False, 'size_bytes': None, 'mtime_ns': None}
    return {'path': str(path), 'exists': True, 'size_bytes': stat.st_size,
            'mtime_ns': stat.st_mtime_ns}


def document_reason_rows(person, statement, testimony, victim_context, privacy):
    """All raw heuristic matches, not culpability; overlapping reasons stay separate."""
    for reason, docs in (('statement', statement), ('testimony', testimony),
                         ('lexical_victim_context', victim_context), ('privacy', privacy)):
        for efta in sorted(docs):
            yield person, efta, reason


def write_ranking_tables(db, rows, reasons):
    """Persist legacy ranking schema even when empty, plus exhaustive provenance."""
    db.execute(f'CREATE TABLE ranking({", ".join(RANKING_COLUMNS)})')
    db.executemany(f'INSERT INTO ranking VALUES ({",".join("?" * len(RANKING_COLUMNS))})',
                   [tuple(row[c] for c in RANKING_COLUMNS) for row in rows])
    db.execute('CREATE TABLE document_reasons('
               'entity_id INTEGER NOT NULL, efta TEXT NOT NULL, reason TEXT NOT NULL, '
               'PRIMARY KEY(entity_id, efta, reason))')
    db.executemany('INSERT INTO document_reasons VALUES (?,?,?)', reasons)
    db.execute('CREATE INDEX document_reasons_document ON document_reasons(efta, entity_id, reason)')
    db.execute('CREATE INDEX document_reasons_reason ON document_reasons(reason, entity_id, efta)')


def main():
    started_at = datetime.now(timezone.utc).isoformat()
    inputs = {name: input_stamp(path) for name, path in {
        'entities': OUT / 'normalized_entities.json',
        'relationships': OUT / 'normalized_relationships.json',
        'person_registry': ROOT / 'data/raw/persons_registry.json',
        'mentions_and_search_phrases': OUT / 'comention_graph.db',
        'corpus': corpus.PATH,
        'page_map_cache': OUT / 'corpus_page_map.npz',
        'survivor_policy': ROOT / 'config/survivors.json',
        'document_urls': ROOT / 'data/raw/VERIFICATION_URLS.csv.gz',
    }.items()}
    entities = json.loads((OUT / 'normalized_entities.json').read_text())
    relationships = json.loads((OUT / 'normalized_relationships.json').read_text())
    by_id = {e['id']: e for e in entities}
    excluded = survivors(entities, relationships)
    registry = {r['name'].casefold(): r for r in json.loads((ROOT / 'data/raw/persons_registry.json').read_text())}
    comention = sqlite3.connect(f'file:{OUT / "comention_graph.db"}?mode=ro', uri=True)
    db = corpus.connect()

    people = {i for (i,) in comention.execute("SELECT id FROM entities WHERE entity_type='person'")} - excluded
    phrases = defaultdict(list)
    for entity_id, phrase in comention.execute('SELECT entity_id, phrase FROM search_terms'):
        if entity_id in people:
            phrases[entity_id].append(phrase)
    mentions = defaultdict(set)
    for entity_id, efta in comention.execute("SELECT entity_id, efta_number FROM mentions WHERE entity_id IN (SELECT id FROM entities WHERE entity_type='person')"):
        if entity_id in people:
            mentions[entity_id].add(efta)
    comention.close()

    print('Loading document types, dates and redaction counts', flush=True)
    doc_type, redactions, doc_date = {}, {}, {}
    for doc, kind, date, redacted in db.execute("SELECT id, type, date, json_extract(meta, '$.redactions') FROM documents"):
        doc_type[doc], redactions[doc] = kind, redacted
        if iso_date(date):
            doc_date[doc] = date
    db.close()
    page_doc, doc_names = page_map()  # cached, shared with build_comentions.py

    def docs_of(pages):
        indices = page_doc[pages[pages < len(page_doc)]]
        return {doc_names[i] for i in set(indices[indices >= 0].tolist())}

    # Every FTS query up front, run in parallel threads (SQLite releases the GIL).
    victim_queries, schedule_queries = {}, {}
    for person in sorted(people):
        if mentions[person]:
            victim_queries[person] = ' OR '.join(f'NEAR({fts_phrase(p)} {term}, {VICTIM_WINDOW})' for p in phrases[person] for term in VICTIM_TERMS)
            if person != EPSTEIN:
                schedule_queries[person] = ' OR '.join(f'NEAR({fts_phrase(p)} {term}, {SCHEDULE_WINDOW})' for p in phrases[person] for term in SCHEDULE_TERMS)
    queries = list(PRIVACY_CODES) + [q for q in (*victim_queries.values(), *schedule_queries.values()) if q]
    found = dict(zip(queries, map(docs_of, corpus.parallel_search(queries, workers=FTS_WORKERS))))

    def docs_for(query):
        return found[query]

    privacy_docs = set().union(*(docs_for(code) for code in PRIVACY_CODES))
    universe = set(doc_names)
    classified = {d for d in universe if doc_type.get(d)}
    population_statement = sum(doc_type[d] in STATEMENT_TYPES for d in classified)
    population_testimony = sum(doc_type[d] in TESTIMONY_TYPES for d in classified)
    privacy_docs &= universe
    corpus_mean = corpus_redaction_mean(redactions, universe)

    print(f'Scoring {len(people)} people', flush=True)
    rows, schedule_events, reason_rows = [], [], []
    for person in sorted(people):
        docs = mentions[person]
        if not docs:
            continue
        near = victim_queries[person]
        victim_context = docs & docs_for(near) if near else set()
        statement = {d for d in docs if doc_type.get(d) in STATEMENT_TYPES}
        testimony = {d for d in docs if doc_type.get(d) in TESTIMONY_TYPES}
        privacy = docs & privacy_docs
        reason_rows.extend(document_reason_rows(person, statement, testimony, victim_context, privacy))
        n = len(docs)
        classified_n = len(docs & classified)
        redaction_n, redaction_mean, redaction_ratio = redaction_stats(docs, redactions, universe, corpus_mean)
        evidence = statement | testimony | victim_context
        e, m = by_id[person], metadata(by_id[person])
        reg = registry.get(e['name'].casefold(), {})
        role = m.get('occupation') or reg.get('category') or m.get('person_type') or ''
        rows.append({
            'entity_id': person, 'name': e['name'], 'role_hint': role,
            'professional_role_likely': bool(PROFESSIONAL.search(f"{role} {reg.get('description', '')}")),
            'documents': n, 'victim_evidence_documents': len(evidence),
            'statement_docs_fbi_police_grand_jury': len(statement), 'statement_enrichment': enrichment(len(statement), classified_n, population_statement, len(classified)),
            'sworn_testimony_docs': len(testimony), 'testimony_enrichment': enrichment(len(testimony), classified_n, population_testimony, len(classified)),
            'victim_context_docs': len(victim_context),
            'privacy_coded_docs': len(privacy), 'privacy_enrichment': enrichment(len(privacy), len(docs & universe), len(privacy_docs), len(universe)),
            'classified_documents': classified_n, 'documents_with_redaction_counts': redaction_n,
            'mean_redactions_per_doc': redaction_mean,
            'redaction_ratio_vs_corpus': redaction_ratio,
            'sample_evidence_docs': ' '.join(sorted(evidence, key=lambda d: (doc_type.get(d) not in STATEMENT_TYPES, d))[:6]),
        })
        if person != EPSTEIN:
            near = schedule_queries[person]
            for d in docs & docs_for(near) if near else ():
                if d in doc_date and doc_type.get(d) in SCHEDULE_TYPES:
                    schedule_events.append((doc_date[d], person, 'schedule_document_mention', d))
    # Victim-evidence documents first, then how unusual that is for the person.
    rows.sort(key=lambda r: (-r['victim_evidence_documents'], -(r['statement_enrichment'] or 0), r['name']))
    for rank, row in enumerate(rows, 1):
        row['rank'] = rank

    flight_events = set()
    for r in relationships:
        if r['relationship_type'] != 'traveled_with':
            continue
        for date, provenance in flight_evidence(r):
            for person in (r['source_entity_id'], r['target_entity_id']):
                if person not in excluded and person in by_id and by_id[person]['entity_type'] == 'person' and not by_id[person].get('suspected_parse_artifact'):
                    flight_events.add((date, person, 'flight', provenance))
    events = sorted(flight_events | set(schedule_events))
    # Hubs (Epstein; staff named in schedules on most document dates)
    # would pair with everyone, so they are left out of same-date leads.
    # Frequent visitors such as Leon Black stay in.
    days_present = defaultdict(set)
    for date, person, _, _ in events:
        days_present[person].add(date)
    all_days = {d for d, _, _, _ in events}
    staff = {e['id'] for e in entities if re.search(r'assistant|secretary|staff', metadata(e).get('occupation') or '', re.I)
             or (registry.get(e['name'].casefold()) or {}).get('category') == 'staff'}
    hubs = {EPSTEIN} | {p for p, days in days_present.items() if p in staff and len(days) > 0.15 * len(all_days)}
    by_day = defaultdict(set)
    for date, person, _, _ in events:
        if person not in hubs:
            by_day[date].add(person)
    shared_days = defaultdict(list)
    for date, present in by_day.items():
        if len(present) <= 25:  # very crowded days carry little pairwise signal
            for a, b in combinations(sorted(present), 2):
                shared_days[a, b].append(date)
    flight_pairs = {frozenset((r['source_entity_id'], r['target_entity_id'])) for r in relationships if r['relationship_type'] == 'traveled_with'}
    pairs = sorted(((a, b, days) for (a, b), days in shared_days.items() if len(days) >= 2),
                   key=lambda p: (-len(p[2]), p[0], p[1]))

    db_path = OUT / 'involvement.partial.db'
    db_path.unlink(missing_ok=True)
    with closing(sqlite3.connect(db_path)) as db, db:
        write_ranking_tables(db, rows, reason_rows)
        db.execute('CREATE TABLE events(date TEXT, entity_id INTEGER, name TEXT, event_type TEXT, source TEXT)')
        db.executemany('INSERT INTO events VALUES (?,?,?,?,?)', [(d, p, by_id[p]['name'], t, s) for d, p, t, s in events])
        db.execute('CREATE TABLE copresence(a INTEGER, b INTEGER, shared_days INTEGER, dates TEXT, has_aggregate_traveled_with_link INTEGER)')
        db.executemany('INSERT INTO copresence VALUES (?,?,?,?,?)', [(a, b, len(d), ' '.join(d), frozenset((a, b)) in flight_pairs) for a, b, d in pairs])
        db.execute('CREATE INDEX events_person ON events(entity_id, date)')
    db_path.replace(OUT / 'involvement.db')

    columns = ['rank'] + [c for c in RANKING_COLUMNS if c != 'rank']
    with (OUT / 'involvement_ranking.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    with (OUT / 'presence_timeline.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['date', 'entity_id', 'name', 'event_type', 'source'])
        writer.writerows((d, p, by_id[p]['name'], t, s) for d, p, t, s in events)
    urls = corpus.document_urls({s for _, _, t, s in events if t == 'schedule_document_mention'})
    edges = []
    for a, b, days in pairs[:400]:
        # Aggregate flight links cannot establish shared flights on these dates.
        sources = sorted({s for d, p, t, s in events if p in (a, b) and d in days and t == 'schedule_document_mention'})[:8]
        edges.append({'id': f'copresence_{a}_{b}', 'source_entity_id': a, 'target_entity_id': b,
                      'relationship_type': 'same_day_document_mentions', 'weight': len(days), 'date_first': days[0], 'date_last': days[-1],
                      'metadata': {'meaning': 'Dated flight evidence or mentions in schedule documents dated the same day. Document dates are NOT appointment dates or physical presence; no meeting or shared flight is established.',
                                   'shared_days': days[:50],
                                   'date_semantics': 'flight dates or document dates; not inferred attendance',
                                   'flight_evidence': [dict(date=d, entity_id=p, provenance=json.loads(s))
                                                       for d, p, t, s in events
                                                       if t == 'flight' and p in (a, b) and d in days[:50]],
                                   'sample_documents': [{'efta_number': s, **({'pdf_url': urls[s]} if s in urls else {})} for s in sources]}})
    (OUT / 'copresence_edges.json').write_text(json.dumps(edges, indent=2) + '\n')
    summary = {'run': {
                   'started_at_utc': started_at,
                   'completed_at_utc': datetime.now(timezone.utc).isoformat(),
                   'inputs': inputs,
                   'code_references': [input_stamp(Path(__file__)),
                                       *[input_stamp(ROOT / 'scripts' / name) for name in
                                         ('corpus_index.py', 'corpus.py', 'adapters/efta.py', 'survivor_policy.py')]],
                   'search_parameters': {
                       'engine': 'SQLite FTS5', 'column': 'text_content',
                       'phrase_source': 'comention_graph.db search_terms(entity_id, phrase); eligible person IDs only',
                       'phrase_normalization': 'double-quoted space-joined Python regex \\w+ tokens',
                       'query_template': 'NEAR({quoted_phrase} {term}, {window}); OR across phrases and terms',
                       'victim_terms': VICTIM_TERMS, 'victim_window': VICTIM_WINDOW,
                       'schedule_terms': SCHEDULE_TERMS, 'schedule_window': SCHEDULE_WINDOW,
                       'privacy_queries': list(PRIVACY_CODES),
                       'workers': FTS_WORKERS, 'query_count': len(queries),
                       'copresence': {'staff_hub_day_fraction_exclusive': 0.15,
                                      'maximum_people_per_day': 25, 'minimum_shared_days': 2,
                                      'viewer_edge_limit': 400},
                       'ranking_sort': ['victim_evidence_documents descending',
                                        'statement_enrichment descending (null as zero)', 'name ascending'],
                       'sample_evidence_limit': 6,
                       'statement_types': sorted(STATEMENT_TYPES),
                       'testimony_types': sorted(TESTIMONY_TYPES),
                       'schedule_types': sorted(SCHEDULE_TYPES),
                       'match_policy': 'Same-page FTS matches mapped to documents, intersected with person mentions. Privacy matches anywhere in a mentioned document. Schedule excludes Epstein.',
                   }},
               'document_reason_provenance': {
                   'database': str(OUT / 'involvement.db'), 'table': 'document_reasons',
                   'columns': ['entity_id', 'efta', 'reason'], 'rows': len(reason_rows),
                   'categories': {
                       'statement': 'Raw document classification in statement_types; not proof this person made a statement.',
                       'testimony': 'Raw document classification in testimony_types; not proof this person testified.',
                       'lexical_victim_context': 'Raw same-page name/term FTS proximity; no semantic role or victim contact established.',
                       'privacy': 'Raw document-level printed FOIA privacy-code match; no inference about redacted text or a person.',
                   },
                   'policy': 'All matches retained, including overlapping reasons and privacy-only documents; samples are not exhaustive.'},
               'interpretation_warning': INTERPRETATION_WARNING,
               'people_ranked': len(rows), 'copresence_hubs_excluded': sorted(by_id[h]['name'] for h in hubs if h in by_id), 'survivors_excluded': len(excluded), 'events': len(events),
               'flight_events': len(flight_events), 'schedule_events': len(schedule_events),
               'corpus_documents': len(universe), 'classified_corpus_documents': len(classified),
               'redaction_counted_corpus_documents': sum(redactions.get(d) is not None for d in universe),
               'enrichment_policy': 'Classification enrichment uses classified documents only. Privacy enrichment uses the indexed document universe. Redaction means exclude missing counts; absent or zero baselines yield null ratios.',
               'timeline_policy': 'Schedule dates are document dates, not appointment dates or attendance. Flight sample dates are incomplete aggregate metadata; provenance is preserved in event sources.',
               'copresence_pairs_2plus_days': len(pairs), 'viewer_copresence_edges': len(edges),
               'warning': 'Proximity to victim evidence is not evidence of wrongdoing. Attorneys, investigators and witnesses appear in the same documents.'}
    (OUT / 'involvement_summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
