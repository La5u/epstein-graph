"""Ranked leads: who to read about next, with the documents behind every number.

    .venv/bin/python scripts/build_leads.py

One row per identity (KG person or email-header identity), never Epstein and
never a survivor (survivor_policy, fails closed). Signals:
  direct_messages        distinct emails between the identity and Epstein with at most
                         3 people on them (mass mail and newsletters are not contact)
  direct_after_2008      the same, dated after his 2008-06-30 plea
  wrote_to_epstein       direct emails the identity sent
  broadcast_messages     larger emails shared with Epstein (shown, barely scored)
  flight_days_with_epstein  days both are on a flight record (involvement.db events)
  schedule_mentions      calendar / schedule documents naming the identity
  shell_companies        Epstein shell companies sharing >= 3 documents (company_graph pairs)
  victim_context_docs    involvement.db: documents with victim-evidence context (KG persons)
  subpoena_docs          the identity's documents that are DOJ subpoenas or returns
DOJ removing or altering documents is NOT a signal: removals mostly withhold
victims' names, and scoring them put young women at the top. Instead
`docs_mostly_withheld` = 1 marks identities with >= WITHHELD_SHARE (at least
WITHHELD_MIN) of their documents removed or altered: they are ranked like everyone
else, but a victim may be among them (most such rows are women's first names; some
are men, a prosecutor among them).
A high score means "read these documents", not wrongdoing: staff, lawyers and
pilots score high for doing their jobs (role column). People who only wrote to
Epstein may themselves be victims nobody has listed; such rows get
`only_contact` = 1 and should be read with that in mind.

Writes output/leads.csv + output/leads_summary.json (KG names are public, other
identities stay pseudonymous labels) and data/private/leads.csv (real names).
"""
import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from survivor_policy import metadata, survivor_ids
from victim_policy import participant_ids, victim_names

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'
PRIVATE = ROOT / 'data/private'
EPSTEIN = 'kg:1'
PLEA = '2008-06-30'
DATE_RANGE = ('1990-01-01', '2019-08-11')
DIRECT_MAX_PEOPLE = 3
SHELL_MIN_DOCS = 3
SAMPLES = 5
WITHHELD_SHARE, WITHHELD_MIN = 0.25, 10
WEIGHTS = {'direct_after_2008': 2.0, 'direct_messages': 1.0, 'wrote_to_epstein': 0.5, 'broadcast_messages': 0.2,
           'flight_days_with_epstein': 2.0, 'schedule_mentions': 1.0, 'shell_companies': 1.5,
           'victim_context_docs': 1.5, 'subpoena_docs': 0.5}
SIGNALS = list(WEIGHTS)


def connect(path):
    return closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True))


def score(row):
    return round(sum(w * math.log1p(row.get(k, 0)) for k, w in WEIGHTS.items()), 3)


def messages(db):
    """{(efta, index): (sender, recipients, date)} from the canonical email headers."""
    out = {}
    for efta, index, source, target, kind, date in db.execute(
            "SELECT efta_number, message_index, canonical_source, canonical_target, relationship_type, date_sent "
            "FROM evidence WHERE relationship_type IN ('sent_to', 'cc_to')"):
        sender, recipients, when = out.setdefault((efta, index), [source, set(), None])
        recipients.add(target)
        if date and DATE_RANGE[0] <= date[:10] <= DATE_RANGE[1]:
            out[efta, index][2] = max(when or '', date[:10])
    return out


def email_signals(db, rows, samples, docs):
    seen = set()
    for (efta, _), (sender, recipients, date) in sorted(messages(db).items(), key=lambda m: (m[0][0] or "", m[0][1] or 0)):
        people = {sender} | recipients
        for person in people:
            docs[person].add(efta)
        if EPSTEIN not in people:
            continue
        key = (sender, frozenset(recipients), date) if date else (efta, sender, frozenset(recipients))
        if key in seen:  # the same email quoted in a later reply or another copy
            continue
        seen.add(key)
        direct = len(people) <= DIRECT_MAX_PEOPLE
        for person in people - {EPSTEIN}:
            row = rows[person]
            if not direct:
                row['broadcast_messages'] += 1
                continue
            row['direct_messages'] += 1
            samples[person]['direct_messages'].append(efta)
            if date and date > PLEA:
                row['direct_after_2008'] += 1
                samples[person]['direct_after_2008'].append(efta)
            if person == sender:
                row['wrote_to_epstein'] += 1


def presence_signals(db, rows, samples):
    flights = defaultdict(set)
    for entity, kind, date in db.execute('SELECT entity_id, event_type, date FROM events'):
        key = f'kg:{entity}'
        if kind == 'flight':
            flights[key].add(date)
        elif kind == 'schedule_document_mention':
            rows[key]['schedule_mentions'] += 1
    for key, dates in flights.items():
        if key != EPSTEIN and (shared := dates & flights[EPSTEIN]):
            rows[key]['flight_days_with_epstein'] = len(shared)
            samples[key]['flight_days_with_epstein'] = sorted(shared)
    roles = {}
    for entity, victim_docs, professional, role, sample in db.execute(
            'SELECT entity_id, victim_evidence_documents, professional_role_likely, role_hint, sample_evidence_docs FROM ranking'):
        key = f'kg:{entity}'
        rows[key]['victim_context_docs'] = victim_docs or 0
        samples[key]['victim_context_docs'] = (sample or '').split()
        roles[key] = (role, bool(professional))
    return roles


def company_signals(db, rows, samples):
    shells = dict(db.execute("SELECT id, name FROM entities WHERE entity_type = 'shell_company'"))
    for company, person, shared in db.execute('SELECT a, b, shared_documents FROM pairs WHERE shared_documents >= ?', (SHELL_MIN_DOCS,)):
        if company in shells and person.isdigit():
            rows[f'kg:{person}']['shell_companies'] += 1
            samples[f'kg:{person}']['shell_companies'].append(shells[company])


def document_signals(rows, samples, docs):
    """Subpoena documents per identity; returns {identity: (removed or altered, documents)}."""
    with connect(OUT / 'comention_graph.db') as db:
        for entity, efta in db.execute('SELECT entity_id, efta_number FROM mentions'):
            docs[f'kg:{entity}'].add(efta)
    with connect(OUT / 'document_info.db') as db:
        flagged = {efta: (status, subpoena) for efta, status, subpoena in db.execute(
            "SELECT efta, release_status, subpoena_target FROM info WHERE release_status IN ('removed', 'altered') "
            "OR subpoena_target IS NOT NULL")}
    withheld = {}
    for person, eftas in docs.items():
        if person not in rows:
            continue
        hits = sorted(eftas & flagged.keys())
        withheld[person] = (sum(flagged[e][0] in ('removed', 'altered') for e in hits), len(eftas))
        for efta in hits:
            if flagged[efta][1]:
                rows[person]['subpoena_docs'] += 1
                samples[person]['subpoena_docs'].append(efta)
    return withheld


def mostly_withheld(counts, public_figure):
    removed, total = counts
    return not public_figure and removed >= WITHHELD_MIN and removed >= WITHHELD_SHARE * total


def excluded_ids(entities, relationships, identity_map):
    """Survivors in the graph, plus every header identity a listed name maps to."""
    ids = {f'kg:{i}' if isinstance(i, int) else i for i in survivor_ids(entities, relationships)}
    for member in participant_ids(victim_names()):
        ids.add(identity_map.get(member, member))
    ids |= {identity_map.get(i, i) for i in list(ids)}
    return ids


def main():
    entities = json.loads((OUT / 'graph_entities.json').read_text())
    relationships = json.loads((OUT / 'graph_relationships.json').read_text())
    rows = defaultdict(Counter)
    samples = defaultdict(lambda: defaultdict(list))
    docs = defaultdict(set)
    with connect(OUT / 'communication_graph.db') as db:
        identity_map = dict(db.execute('SELECT member_id, canonical_id FROM identity_map'))
        labels = dict(db.execute('SELECT id, label FROM nodes'))
        email_signals(db, rows, samples, docs)
    with connect(OUT / 'involvement.db') as db:
        roles = presence_signals(db, rows, samples)
    with connect(OUT / 'company_graph.db') as db:
        company_signals(db, rows, samples)
    withheld = document_signals(rows, samples, docs)

    names, occupations, public_figures = {}, {}, set()
    for e in entities:
        key = f'kg:{e["id"]}' if isinstance(e['id'], int) else e['id']
        names[key] = e['name']
        occupations[key] = metadata(e).get('occupation') or metadata(e).get('person_type')
        if metadata(e).get('public_figure'):
            public_figures.add(key)
    excluded = excluded_ids(entities, relationships, identity_map) | {EPSTEIN}
    secret = PRIVATE / 'viewer_secret.json'
    reveal = json.loads(secret.read_text()).get('reveal', {}) if secret.exists() else {}

    table = []
    for key, row in rows.items():
        if key in excluded or not key or not any(row.values()):
            continue
        kg = key.startswith('kg:')
        role, professional = roles.get(key, (occupations.get(key), False))
        signals = {k: row.get(k, 0) for k in SIGNALS}
        contact_only = not any(signals[k] for k in SIGNALS if k not in ('direct_messages', 'direct_after_2008', 'wrote_to_epstein', 'broadcast_messages'))
        table.append({'id': key, 'name': names.get(key) if kg else labels.get(key, names.get(key, key)),
                      'private_name': (names.get(key) if kg else reveal.get(key, {}).get('value')) or labels.get(key, key),
                      'role': role or '', 'professional_role_likely': int(bool(professional)),
                      'score': score(signals), 'signal_kinds': sum(1 for k in SIGNALS if k != 'broadcast_messages' and signals[k]),
                      **signals, 'only_contact': int(contact_only),
                      'docs_mostly_withheld': int(mostly_withheld(withheld.get(key, (0, 0)), key in public_figures)),
                      'evidence': json.dumps({k: v[:SAMPLES] for k, v in samples[key].items() if v}, ensure_ascii=False)})
    table = [r for r in table if r['score'] > 0]
    table.sort(key=lambda r: (-r['score'], r['id']))
    for rank, r in enumerate(table, 1):
        r['rank'] = rank

    public = ['rank', 'id', 'name', 'role', 'professional_role_likely', 'score', 'signal_kinds', *SIGNALS, 'only_contact', 'docs_mostly_withheld', 'evidence']
    write(OUT / 'leads.csv', public, table)
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    write(PRIVATE / 'leads.csv', ['rank', 'id', 'private_name', *public[3:]], table)
    (PRIVATE / 'leads.csv').chmod(0o600)
    summary = {'identities': len(table), 'excluded_survivor_ids': len(excluded) - 1, 'docs_mostly_withheld': sum(r['docs_mostly_withheld'] for r in table),
               'with_signal': {k: sum(1 for r in table if r[k]) for k in SIGNALS},
               'two_or_more_kinds': sum(1 for r in table if r['signal_kinds'] >= 2),
               'top': [{k: r[k] for k in ('rank', 'id', 'name', 'score', 'signal_kinds')} for r in table[:25]]}
    (OUT / 'leads_summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({k: v for k, v in summary.items() if k != 'top'}, indent=2))


def write(path, columns, table):
    partial = path.with_suffix('.partial.csv')
    with partial.open('w', newline='') as f:
        writer = csv.DictWriter(f, columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(table)
    partial.replace(path)


if __name__ == '__main__':
    main()
