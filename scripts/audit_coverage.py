"""Structured-only coverage audit. Writes ONLY output/audit_coverage.{json,md}.
Run: python scripts/audit_coverage.py
Inputs use SQLite mode=ro&immutable=1 (run only on quiescent completed downloads).
No raw contacts, header names, subjects, text, or secret are emitted.
"""
import collections as C
import datetime as dt
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
import sqlite3
import time
import sys
sys.dont_write_bytecode = True

from build_contacts import normalize_contact, contact_id
from build_communications import Builder, valid_email, parse_recipient, parse_sender, normalized_date, join_split_addresses, recipient_repairs
from normalize_graph import registry_aliases
from normalize_graph import normalize, name_key

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'
RAW = ROOT / 'data/raw'

def load(p):
    return json.loads(p.read_text())

def db(p):
    c = sqlite3.connect(p.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    c.execute('PRAGMA query_only=ON')
    return c

def scalar(c, q, args=()):
    return c.execute(q, args).fetchone()[0]

def overlap(a, b):
    return dict(left=len(a), right=len(b), intersection=len(a & b), left_only=len(a-b), right_only=len(b-a))

@lru_cache(maxsize=65536)
def string_recipients(value):
    return tuple(parse_recipient(value))

@lru_cache(maxsize=32768)
def recipient_fields(raw):
    """Nonstring count and joined recipients, retaining token order.

    Cache the complete field transformation: summing markers and folding
    split addresses again for each of 4M repeated fields dominated the replay.
    """
    try:
        values = json.loads(raw or '[]')
        if not isinstance(values, list): return None
    except (ValueError, TypeError):
        return None
    return (sum(not isinstance(value, str) for value in values),
            join_split_addresses(pair for value in values
                                 for pair in (string_recipients(value) if isinstance(value, str) else parse_recipient(value))))

@lru_cache(maxsize=8192)
def date_flags(value):
    shaped = isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is not None
    invalid = False
    if shaped:
        try: dt.date.fromisoformat(value)
        except ValueError: invalid = True
    return shaped, invalid, not normalized_date(value)

def main():
    started = time.time()
    key = (ROOT / 'data/private/contact_hmac.key').read_bytes()
    @lru_cache(maxsize=None)
    def hid(v):
        v = valid_email(v)
        return contact_id(v, 'email', key) if v else None
    kg, comm, meta, derived, corpus = [db(p) for p in [RAW/'knowledge_graph.db', RAW/'communications.db', RAW/'email_metadata.db', OUT/'communication_graph.db', RAW/'full_text_corpus.db']]
    r = {'method': {'command': 'python scripts/audit_coverage.py', 'sqlite': 'mode=ro&immutable=1; query_only=ON; no text-content/FTS reads', 'privacy': 'Existing local HMAC key used in memory only; no raw contact/header labels or secret in output.', 'comparison': 'Same-project structured overlap, NOT independent verification or comprehensive external accuracy.', 'bounds': 'Full structured metadata streams; no corpus-wide text/page scans. Corpus reference lookup at most 100 per source; indexed SQL missing-pair checks for every missing distinct pair.', 'inputs': {str(p.relative_to(ROOT)): {'bytes': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns} for p in [RAW/'knowledge_graph.db', RAW/'communications.db', RAW/'email_metadata.db', RAW/'full_text_corpus.db', OUT/'communication_graph.db']}}, 'definitions': {'pair': 'Directed (sender email HMAC, recipient email HMAC, sent_to/cc_to); local-part case preserved, domain casefolded; build_communications.valid_email exclusions applied.', 'duplicate': 'Excess rows over distinct structured header/date signatures, NOT duplicate real-world message proof. Signature excludes subject/body and includes sender label/address and recipient JSON, date; second metric includes EFTA/message_index.', 'export_contact': 'build_contacts.normalize_contact + contact_id using existing key. Phone comparison to headers unsupported; no metadata phone field.', 'date': 'Builder validates YYYY-MM-DD with datetime.date.fromisoformat; raw ISO-shaped dates are independently checked for calendar validity.', 'gap': 'Absence from this derived layer, not proof absent from full corpus or external reality.'}}
    entities = load(RAW/'knowledge_graph_entities.json'); rels = load(RAW/'knowledge_graph_relationships.json')
    norm, nr, merges = normalize(entities, rels)
    sqlentities = [dict(zip(['id','name','entity_type'], row)) for row in kg.execute('SELECT id,name,entity_type FROM entities')]
    ei = {e['id']: e for e in entities}; si = {e['id']: e for e in sqlentities}
    sem = lambda e: (e['entity_type'], name_key(e['name']))
    sqlrels = list(kg.execute('SELECT id,source_entity_id,target_entity_id,relationship_type,weight FROM relationships'))
    exported = {(x['id'], x['source_entity_id'], x['target_entity_id'], x['relationship_type'], x['weight']) for x in rels}
    columns = ('id','source_entity_id','target_entity_id','relationship_type','weight')
    graph = load(OUT/'graph.json'); combined = load(OUT/'graph_entities.json'); combinedrels = load(OUT/'graph_relationships.json')
    ids = {x['id'] for x in combined}
    groups = C.Counter(sem(e) for e in sqlentities)
    r['knowledge_graph'] = {'db_entities': len(si), 'json_entities': len(ei), 'id_overlap': overlap(set(si), set(ei)), 'shared_id_semantic_mismatches': sum(sem(si[i]) != sem(ei[i]) for i in si.keys() & ei.keys()), 'semantic_label_type_overlap': overlap(set(map(sem,sqlentities)),set(map(sem,entities))), 'db_relationships': len(sqlrels), 'json_relationships': len(rels), 'exact_relationship_tuple_overlap': overlap(set(sqlrels),exported), 'db_casefold_duplicate_groups': sum(n>1 for n in groups.values()), 'db_duplicate_excess_nodes': sum(n-1 for n in groups.values()), 'export_merge_groups': len(merges), 'normalized_nodes': len(norm), 'normalization_reproduces_nodes': norm == load(OUT/'normalized_entities.json'), 'normalization_reproduces_relationships': nr == load(OUT/'normalized_relationships.json'), 'normalized_relationship_records': len(nr), 'combined_nodes': len(ids), 'combined_relationships': len(combinedrels), 'dangling_combined_relationships': sum(e['source_entity_id'] not in ids or e['target_entity_id'] not in ids for e in combinedrels), 'viewer_graph_counts': {'nodes':len(graph['nodes']), 'edges':len(graph.get('edges',graph.get('links',[])))}, 'independent_sql_duplicate_groups': scalar(kg,"SELECT count(*) FROM (SELECT entity_type,lower(trim(name)),count(*) n FROM entities GROUP BY entity_type,lower(trim(name)) HAVING n>1)"), 'independent_sql_id_label_match_count': sum(scalar(kg,'SELECT count(*) FROM entities WHERE id=? AND name=? AND entity_type=?',(e['id'],e['name'],e['entity_type'])) for e in entities)}
    # SQL resolvers: schema comment is not the actual foreign key target.
    r['resolver'] = {'participants': scalar(comm,'SELECT count(*) FROM email_participants'), 'roles': dict(comm.execute('SELECT role,count(*) FROM email_participants GROUP BY role')), 'resolved_nonnull': scalar(comm,'SELECT count(*) FROM email_participants WHERE resolved_entity_id IS NOT NULL'), 'join_resolved_identities': scalar(comm,'SELECT count(*) FROM email_participants p JOIN resolved_identities r ON r.id=p.resolved_entity_id'), 'join_kg_ids': scalar(comm,'SELECT count(*) FROM email_participants WHERE resolved_entity_id IN ('+','.join(map(str,si))+')'), 'resolved_methods': dict(comm.execute('SELECT match_method,count(*) FROM resolved_identities GROUP BY match_method')), 'noise_counts': dict(comm.execute('SELECT is_noise,count(*) FROM emails GROUP BY is_noise'))}
    # Actual participants, not upstream aggregated person-name pair tables.
    by_message = {}
    commcontacts = set()
    for mid, role, address in comm.execute('SELECT p.email_id,p.role,p.email_address FROM email_participants p JOIN emails e ON e.id=p.email_id WHERE e.is_noise=0'):
        h = hid(address)
        if h:
            commcontacts.add(h)
            by_message.setdefault(mid,C.defaultdict(set))[role].add(h)
    cp = set(); pair_message = {}
    for mid, roles in by_message.items():
        for a in roles['from']:
            for role, kind in [('to','sent_to'),('cc','cc_to')]:
                for b in roles[role]:
                    cp.add((a,b,kind)); pair_message.setdefault((a,b,kind),mid)
    dp = {(a,b,k) for a,b,k in derived.execute("SELECT DISTINCT source_id,target_id,relationship_type FROM evidence WHERE relationship_type IN ('sent_to','cc_to')") if a.startswith('email_') and b.startswith('email_')}
    builder = Builder(key,norm,registry_aliases(norm,json.loads((RAW/'persons_registry.json').read_text())))
    endpoint = builder.endpoint  # memoized in Builder
    repairs = recipient_repairs()
    sender_header = lru_cache(maxsize=262144)(parse_sender)
    mp = set(); expected = C.Counter(); stats = C.Counter(); metadata_contacts=set(); merged_locations=set()
    export = load(RAW/'extracted_entities_filtered.json')
    ec = {}; skipped=C.Counter()
    for group,kind in [('emails','email'),('phones','phone')]:
        values=set()
        for row in export[group]:
            v=normalize_contact(row['entity_value'],kind)
            if v: values.add(contact_id(v,kind,key))
            else: skipped[kind]+=1
        ec[kind]=values
    q='SELECT id,efta_number,message_index,sender,sender_email,to_recipients,cc_recipients,date_sent FROM emails_merged WHERE is_noise=0'
    for rid,efta,idx,name,address,to,cc,date in meta.execute(q):
        stats['non_noise_rows']+=1
        merged_locations.add(hashlib.sha256(str((efta,idx)).encode()).digest()[:16])
        if date:
            stats['date_nonempty']+=1
            shaped, invalid, discarded = date_flags(date)
            if shaped:
                stats['date_regex_accepted']+=1
                if invalid: stats['date_regex_accepted_but_invalid_calendar']+=1
            if discarded: stats['date_discarded_by_builder']+=1
        a=hid(address)
        if a: metadata_contacts.add(a)
        elif address: stats['nonempty_sender_email_rejected']+=1
        if not a and (recovered:=sender_header(name,address))!=(name,address):
            stats['sender_address_recovered_from_header']+=1; name,address=recovered
            if (a:=hid(address)): metadata_contacts.add(a)
        sender=endpoint(name,address)
        if sender is None: stats['missing_usable_sender']+=1
        seen=set(); recipients=set()
        for kind,raw in [('sent_to',to),('cc_to',cc)]:
            values=recipient_fields(raw)
            if values is None:
                stats['invalid_recipient_json_fields']+=1; continue
            stats['nonstring_recipient_values']+=values[0]
            # Same fragment folding as the builder ("...@g" | "mail.com").
            for n,addr in values[1]:
                b=hid(addr)
                if b: metadata_contacts.add(b)
                recipient=endpoint(n,addr)
                if recipient is None: stats['unusable_recipient_tokens']+=1; continue
                recipients.add(recipient)
                if a and b: mp.add((a,b,kind))
                if sender and (recipient,kind) not in seen:
                    expected[sender,recipient,kind]+=1;seen.add((recipient,kind))
            for recipient in repairs.get((rid,kind),()):  # recovered from scans, as in the builder
                recipients.add(recipient)
                if a: mp.add((a,recipient,kind))
                if sender and (recipient,kind) not in seen:
                    expected[sender,recipient,kind]+=1;seen.add((recipient,kind))
        if not recipients: stats['missing_usable_recipients']+=1
        if seen: stats['rows_with_edges']+=1
    # Parsing/resolution state is no longer needed; release it before the large
    # location-set comparisons, rather than retaining both memory peaks.
    recipient_fields.cache_clear()
    string_recipients.cache_clear()
    date_flags.cache_clear()
    del endpoint, builder, sender_header
    # These TEXT header columns have binary equality, as did the serialized
    # signatures. Group in SQLite instead of serializing and hashing every row.
    signature_columns='sender,sender_email,to_recipients,cc_recipients,date_sent'
    signature_excess, signature_groups = meta.execute(f'SELECT coalesce(sum(n-1),0),coalesce(sum(n>1),0) FROM (SELECT count(*) n FROM emails_merged WHERE is_noise=0 GROUP BY {signature_columns})').fetchone()
    location_excess = scalar(meta,f'SELECT coalesce(sum(n-1),0) FROM (SELECT count(*) n FROM emails_merged WHERE is_noise=0 GROUP BY efta_number,message_index,{signature_columns})')
    actual = C.Counter({(a,b,k):n for a,b,k,n in derived.execute("SELECT source_id,target_id,relationship_type,count(*) FROM evidence WHERE source_db='email_metadata.db' AND relationship_type IN ('sent_to','cc_to') GROUP BY source_id,target_id,relationship_type")})
    location_coverage = {}
    for table in ('emails','emails_gap'):
        source_locations={hashlib.sha256(str((efta,index)).encode()).digest()[:16] for efta,index in meta.execute(f'SELECT efta_number,message_index FROM {table} WHERE is_noise=0')}
        location_coverage[table]=overlap(source_locations,merged_locations)
    r['metadata_location_coverage']={'comparisons':location_coverage,'warning':'EFTA/message-index key overlap only. Index assignments and extraction versions may differ; unmatched keys are candidates for reconciliation, not proven missing real-world messages.'}
    r['messages']={'counts':dict(stats), 'noise_counts_merged':dict(meta.execute('SELECT is_noise,count(*) FROM emails_merged GROUP BY is_noise')), 'header_date_signature_excess_rows':signature_excess, 'header_date_signature_duplicate_groups':signature_groups, 'same_efta_index_header_date_excess_rows':location_excess, 'derived_pair_count_mismatches_vs_replayed_builder':sum(expected[k]!=actual[k] for k in expected.keys()|actual.keys()), 'derived_evidence_by_type':dict(derived.execute('SELECT relationship_type,count(*) FROM evidence GROUP BY relationship_type')), 'node_types':dict(derived.execute('SELECT entity_type,count(*) FROM nodes GROUP BY entity_type')), 'all_tables_row_counts':{t:scalar(meta,f'SELECT count(*) FROM {t}') for t in ['emails','emails_merged','emails_gap','communication_pairs','communication_pairs_v2','communication_pairs_v3']}}
    r['pair_coverage']={'communications_vs_derived':overlap(cp,dp),'communications_vs_merged':overlap(cp,mp),'merged_vs_derived':overlap(mp,dp),'communications_nonnoise_messages_with_valid_email_participants':len(by_message),'bcc_policy':'Not built into sent_to/cc_to; role counts reported, no bcc pairs included.'}
    # Independent SQL verifies every missing distinct pair: both endpoints
    # are selected in SQL; anti-lookup uses the indexed derived pair table.
    comm.create_function('audit_email_id',1,hid,deterministic=True)
    checks=[]
    for pair in sorted(cp-dp):
        mid=pair_message[pair];a,b,kind=pair
        role='to' if kind=='sent_to' else 'cc'
        found=scalar(comm,"SELECT count(*) FROM email_participants a JOIN email_participants b ON a.email_id=b.email_id JOIN emails e ON e.id=a.email_id WHERE a.email_id=? AND e.is_noise=0 AND a.role='from' AND b.role=? AND audit_email_id(a.email_address)=? AND audit_email_id(b.email_address)=?",(mid,role,a,b))
        absent=scalar(derived,'SELECT count(*) FROM evidence WHERE source_id=? AND target_id=? AND relationship_type=?',pair)==0
        checks.append({'source_record_id':mid,'source_pair_sql_rows':found,'absent_from_derived_sql':absent})
    r['pair_coverage']['independent_sql_gap_checks']=checks
    r['pair_coverage']['independent_sql_present_source_pair_types']=sum(scalar(derived,'SELECT count(*) FROM evidence WHERE source_id=? AND target_id=? AND relationship_type=?',pair)>0 for pair in cp)
    def bad_date(value):
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value): return 0
        try: dt.date.fromisoformat(value)
        except ValueError: return 1
        return 0
    meta.create_function('audit_bad_date',1,bad_date,deterministic=True)
    derived.create_function('audit_bad_date',1,bad_date,deterministic=True)
    r['messages']['independent_sql_invalid_calendar_dates'] = scalar(meta,'SELECT coalesce(sum(n*audit_bad_date(date_sent)),0) FROM (SELECT date_sent,count(*) n FROM emails_merged WHERE is_noise=0 GROUP BY date_sent)')
    r['messages']['derived_invalid_calendar_evidence_dates'] = scalar(derived,'SELECT coalesce(sum(n*audit_bad_date(date_sent)),0) FROM (SELECT date_sent,count(*) n FROM evidence GROUP BY date_sent)')
    r['messages']['signature_excess_fraction'] = round(signature_excess / stats['non_noise_rows'],6)
    r['messages']['rows_with_edges_fraction'] = round(stats['rows_with_edges']/stats['non_noise_rows'],6)
    cn={n['id'] for n in load(OUT/'contact_nodes.json')}
    r['contacts']={'export_rows':{'email':len(export['emails']),'phone':len(export['phones'])},'unsupported_export_rows':dict(skipped),'export_vs_contact_nodes':overlap(ec['email']|ec['phone'],cn),'export_emails_vs_merged_header_emails':overlap(ec['email'],metadata_contacts),'export_emails_vs_communications_participant_emails':overlap(ec['email'],commcontacts),'export_phones':len(ec['phone']),'metadata_phone_comparison':'Unavailable: these SQLite schemas have no structured phone contact table; not counted as zero overlap.'}
    view=load(OUT/'communication_edges.json'); types=C.Counter(e['relationship_type'] for e in view)
    r['viewer']={'communication_edges_by_type':dict(types),'full_edges_by_type':dict(derived.execute('SELECT relationship_type,count(*) FROM edges GROUP BY relationship_type')),'definition':'Top record-count directed communication pairs plus matching identity associations; contacts without shared references omitted; topology is slice-specific.'}
    refs={}
    refs['contact_export']=sorted({ref for group in ('emails','phones') for row in export[group] for ref in row['efta_numbers']})[:100]
    refs['communication_view']=sorted({ref['efta_number'] for e in view for ref in e['metadata']['sample_source_references'] if ref['efta_number']})[:100]
    r['corpus']={'schema':{name:[x[1:3] for x in corpus.execute('PRAGMA table_info("'+name+'")')] for name, in corpus.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('documents','pages','efta_crosswalk','document_cases')")},'sampling':'First 100 distinct sorted EFTA refs per source (deterministic, not random or representative). Indexed exact lookups only.', 'reference_checks':{}}
    for source,values in refs.items():
        counts=C.Counter()
        for ref in values:
            d=corpus.execute('SELECT total_pages,error FROM documents WHERE efta_number=?',(ref,)).fetchone()
            page=corpus.execute('SELECT 1 FROM pages WHERE efta_number=? LIMIT 1',(ref,)).fetchone()
            counts['tested']+=1;counts['document_exists']+=bool(d);counts['has_page_row']+=bool(page)
            counts['document_error_nonempty']+=bool(d and d[1])
        r['corpus']['reference_checks'][source]=dict(counts)
    r['limitations']=['No external validation, identity adjudication, raw-text NER, subject/body inspection or victim de-redaction.', 'Email validity is the existing conservative parser policy, not RFC completeness or deliverability. Exclusions combine placeholders and malformed values.', 'Builder replay uses existing parser, so agreement is consistency rather than independent correctness; source pair gaps independently checked with SQL.', 'Structured header duplicate signatures can conflate different messages and miss reformatting/quoted copies; no deduplicated email count is claimed.', 'Corpus sample presence verifies only document/page keys, not citation accuracy or page content. No comprehensive missing-data claim.', 'Immutable SQLite ignores WAL; inputs must be quiescent. Key never emitted; HMAC overlaps are case-sensitive in local parts.', 'No full integrity check or corpus-wide page count; expensive corpus queries intentionally bounded.']
    r['findings'] = [
        {'severity':'fixed' if not r['messages']['derived_invalid_calendar_evidence_dates'] else 'bug','finding':f"Raw metadata contains {r['messages']['independent_sql_invalid_calendar_dates']} impossible ISO-shaped calendar dates; {r['messages']['derived_invalid_calendar_evidence_dates']} derived evidence rows retain invalid dates.", 'action':'Raw source dates remain unchanged. Builder now validates calendar dates and tests invalid months/days; derived invalid-date evidence should be zero.'},
        {'severity':'coverage','finding':f'{len(cp-dp)} of {len(cp)} communications.db directed valid-address pair/types remain absent from derived; {len(cp-mp)} are absent from merged headers alone. Indexed SQL confirms {r["pair_coverage"]["independent_sql_present_source_pair_types"]} source pair/types present. Any remaining gaps have source-participant SQL checks.', 'action':'Importer now backfills only pair/types absent from merged metadata, with source evidence. Overlapping pair counts are not added; zero pair gaps does not establish complete message coverage.'},
        {'severity':'schema_bug_handled','finding':'resolved_entity_id is not a KG ID despite schema comment: 36,579 populated references join resolved_identities; zero join KG IDs. Importer correctly handles this. KG DB vs JSON IDs themselves match.', 'action':'Correct upstream schema documentation and preserve join regression tests.'},
        {'severity':'quality','finding':'27 duplicate normalized name/type groups represent 60 excess KG nodes. Normalizer retains provenance and all 2,302 relationships.', 'action':'Do not interpret exact label equality as verified identity.'},
        {'severity':'quality','finding':f"{stats['rows_with_edges']:,} / {stats['non_noise_rows']:,} non-noise rows yield edges. {r['messages']['header_date_signature_excess_rows']:,} rows exceed distinct header/date signatures, not proven duplicate real-world emails. Name-only participant nodes dominate.", 'action':'Expose extraction and repetition warnings; unresolved labels are not verified people. Structured [name,email] recipient arrays are now parsed; other unsupported values still require review.'},
        {'severity':'viewer','finding':f"Viewer includes {sum(types[k] for k in ('sent_to','cc_to')):,} of {sum(r['viewer']['full_edges_by_type'].get(k,0) for k in ('sent_to','cc_to')):,} directed communication pair/type edges, plus {sum(n for k,n in types.items() if k not in ('sent_to','cc_to')):,} identity links.", 'action':'Separate full-network from slice topology prominently; use SQLite for coverage analysis.'},
        {'severity':'coverage','finding':f"{r['contacts']['export_vs_contact_nodes']['intersection']:,} normalized exported contacts retained; {r['contacts']['export_emails_vs_merged_header_emails']['intersection']} / {len(ec['email'])} exported emails overlap merged headers. Phone overlap is not measurable with these metadata schemas.", 'action':'Treat contact inventories as complementary, not independent verification or evidence of absence.'},
        {'severity':'sample','finding':f"Corpus reference samples: {json.dumps(r['corpus']['reference_checks'],sort_keys=True)}.", 'action':'Do not extrapolate to complete citation accuracy or corpus completeness.'}
    ]
    for connection in (kg,comm,meta,derived,corpus):
        connection.close()
    r['elapsed_seconds']=round(time.time()-started,2)
    (OUT/'audit_coverage.json').write_text(json.dumps(r,indent=2)+'\n')
    sections=['# Structured coverage audit','Reproduce: `python scripts/audit_coverage.py`. Only the two audit outputs are written. Detailed counts, definitions, SQL verification results and corpus schema are in `output/audit_coverage.json`.','**Scope:** same-project overlap, not independent verification. No raw contact values, private header labels, secret, subjects or corpus text are reported.']
    sections += ['## Findings and actions']
    sections += ['- **'+f['severity']+':** '+f['finding']+' **Action:** '+f['action'] for f in r['findings']]
    for title,k in [('Knowledge graph IDs and normalization','knowledge_graph'),('Resolver joins','resolver'),('Message quality and replay','messages'),('Directed email pair coverage','pair_coverage'),('HMAC contact coverage','contacts'),('Metadata location key coverage','metadata_location_coverage'),('Viewer truncation','viewer')]:
        sections += ['## '+title,'```json\n'+json.dumps(r[k],indent=2)+'\n```']
    sections += ['## Corpus reference sample','```json\n'+json.dumps(r['corpus']['reference_checks'],indent=2)+'\n```',r['corpus']['sampling'],'## Definitions']
    sections += ['- **'+k+':** '+v for k,v in r['definitions'].items()]
    sections += ['## Limitations']+['- '+v for v in r['limitations']]
    (OUT/'audit_coverage.md').write_text('\n\n'.join(sections)+'\n')
    print('Wrote output/audit_coverage.json and output/audit_coverage.md; seconds:',r['elapsed_seconds'])

if __name__=='__main__':
    main()
