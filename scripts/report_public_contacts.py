"""Local GDPR review report. Real examples are written ONLY to data/private.

Run after build.py. The optional pre-change snapshot lives in
 data/private/gdpr_review/before{,_canonicals}.json.
"""
from collections import Counter, defaultdict
import base64
import gzip
import hashlib
import json
from pathlib import Path
import random
import re
import sqlite3
import subprocess

from bounded_json import items
from viewer_secret import _write_private

ROOT = Path(__file__).resolve().parents[1]


def viewer_data(html):
    match = re.search(r'id="viewer-data">(.*?)</script>', html, re.S)
    return json.loads(gzip.decompress(base64.b64decode(match[1])))


def main():
    folder = ROOT / 'data/private/gdpr_review'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    before_path = folder / 'before.json'
    before = json.loads(before_path.read_text()) if before_path.exists() else {}
    before_map_path = folder / 'before_canonicals.json'
    before_map = json.loads(before_map_path.read_text()) if before_map_path.exists() else {}
    out = ROOT / 'output'
    with sqlite3.connect(f'file:{out}/contact_graph.db?mode=ro', uri=True) as db:
        rows = db.execute("SELECT r.member,r.canonical,r.rule,r.organisation FROM resolutions r LEFT JOIN nodes n ON n.id=r.member WHERE n.kind IN ('email','phone') OR r.member GLOB 'email_*' OR r.member GLOB 'phone_*'").fetchall()
        ocr = db.execute('SELECT member,canonical,rule FROM aliases').fetchall()
        retained = dict(db.execute("SELECT kind,count(*) FROM nodes WHERE kind IN ('email','phone') GROUP BY kind"))
        remaining = dict(db.execute("SELECT n.kind,count(DISTINCT n.canonical) FROM nodes n JOIN nodes t ON t.id=n.canonical AND t.kind=n.kind WHERE n.kind IN ('email','phone') GROUP BY n.kind"))
        current_ids = {i for i, in db.execute('SELECT id FROM nodes')}
        contact_kinds=dict(db.execute("SELECT id,kind FROM nodes WHERE kind IN ('email','phone')"))
        for identity,_,_,_ in rows:
            if identity.startswith(('email_','phone_')): contact_kinds.setdefault(identity,identity.split('_',1)[0])
        org_ids={i for i, in db.execute('SELECT member FROM resolutions WHERE organisation IS NOT NULL')}
        resolution_summary = json.loads(db.execute('SELECT json FROM summary').fetchone()[0])['resolution']
        proxy_folds=db.execute("SELECT count(*) FROM resolutions WHERE rule='organisation_domain_owner' AND member!=canonical").fetchone()[0]
    targets = {c for _,c,_,_ in rows}
    target_nodes = {}
    graph_counts = Counter()
    for e in items(out / 'graph_entities.json'):
        graph_counts[e['entity_type']] += 1
        if str(e['id']) in targets:
            target_nodes[str(e['id'])] = (e['name'], e['entity_type'])
    for filename in ('normalized_entities.json','company_nodes.json','communication_nodes.json'):
        for e in items(out / filename):
            if str(e['id']) in targets:
                target_nodes[str(e['id'])] = (e['name'],e['entity_type'])
    groups = defaultdict(list)
    totals = Counter()
    new_totals = Counter()
    existing_changes, added_changes = Counter(), Counter()
    for i,c,rule,org in rows:
        if i == c:
            continue
        kind = target_nodes.get(c, ('','participant' if c.startswith('participant_') else ''))[1]
        group = 'organisation' if kind in {'organization','shell_company'} else 'person' if kind in {'person','participant'} else 'ocr_variant'
        totals[group] += 1
        if before_map.get(i,i) != c:
            new_totals[group] += 1
            (existing_changes if i in before_map else added_changes)[group] += 1
            groups[group].append((i,c,rule))
            groups['rule_'+rule].append((i,c,rule))
    groups['ocr_variant'] = ocr
    for row in ocr:
        groups['ocr_'+row[2]].append(row)
    html = (out/'graph.html').read_text()
    public = json.loads(re.search(r'const PUBLIC_REVEAL = (\{.*?\});',html,re.S)[1])
    groups['hashed'] = [(i,v,'HMAC-SHA256') for i,v in public.items() if re.fullmatch(r'(?:email|phone):[a-f0-9]{12}',v)]
    rng = random.Random(20261110)
    sampled = {g:rng.sample(sorted(examples),min(15,len(examples))) for g,examples in sorted(groups.items())}
    wanted = {i for examples in sampled.values() for row in examples for i in row[:2]}
    labels = {i:n for i,(n,_) in target_nodes.items() if i in wanted}
    for filename in ('fulltext_contact_lookup.json','contact_lookup.json','entity_contact_lookup.json'):
        for i,v in items(ROOT / 'data/private' / filename):
            if i in wanted:
                labels[i] = v['value']
    from viewer_label_payload import secret_entries
    for table,i,value in secret_entries(ROOT/'data/private/viewer_secret.json'):
        if table=='reveal':
            if i in wanted: labels[i]=value['value']
            kind=i.split('_',1)[0] if i.startswith(('email_','phone_')) else value.get('kind')
            if kind in {'email','phone'}: contact_kinds.setdefault(i,kind)
    split_by_kind=defaultdict(Counter)
    for i,value in public.items():
        kind=contact_kinds.get(i)
        if kind in {'email','phone'}:
            category='hashed' if re.fullmatch(r'(?:email|phone):[a-f0-9]{12}',value) else 'plaintext_org' if i in org_ids else 'plaintext_named_public_figure'
            split_by_kind[category][kind]+=1
    examples = {g:[{'value':labels.get(i,i),'target':labels.get(c,c),'rule':rule,'id':i,'target_id':c}
                   for i,c,rule in rows_] for g,rows_ in sampled.items()}
    payload = viewer_data(html)
    viewer_counts = Counter(n['record'].get('subtype') if n['record']['entity_type']=='contact' else n['record']['entity_type']
                            for n in payload['nodes'])
    cohort = {i:c for i,c,_,_ in rows if i in before_map}
    cohort_counts = {}
    for kind in ('email','phone'):
        prefix = kind+'_'
        ids = [i for i in before_map if i.startswith(prefix)]
        cohort_counts[kind] = {'previous_nodes':len(ids), 'retained':sum(i in current_ids for i in ids),
            'before_distinct_contact_canonicals':len({before_map[i] for i in ids if before_map[i].startswith(prefix)}),
            'after_distinct_contact_canonicals':len({cohort.get(i,i) for i in ids if cohort.get(i,i).startswith(prefix)}),
            'after_distinct_contacts_by_node_kind':len({cohort.get(i,i) for i in ids if contact_kinds.get(cohort.get(i,i))==kind}),
            'canonical_metric':'Same original members; ID namespace for the comparable before/after pair. Current node-kind count also shown for legacy kind corrections.'}
    result = {'date':'2026-11-10','reason':'GDPR review','before':before,
              'after':{'full_graph':dict(graph_counts),'viewer_nodes':dict(viewer_counts),
                       'retained_contact_nodes':retained,'distinct_contact_canonicals':remaining,
                       'merged_contacts':dict(totals),'changed_canonicals':dict(new_totals),'previous_contact_cohort':cohort_counts,
                       'changed_existing_contact_canonicals':dict(existing_changes),'added_index_resolutions':dict(added_changes),
                       'resolution_scope':'All email/phone resolution records, including header aliases retained in the communication layer. Changed totals include new index members; existing-cohort changes are separate.',
                       'organisation_proxy_folds':proxy_folds,
                       'ocr_aliases':len(ocr),'ocr_by_rule':dict(Counter(rule for _,_,rule in ocr)),
                       'resolution':resolution_summary,
                       'entity_contact_redactions':json.loads((out/'entity_contact_summary.json').read_text()),
                       'public_split':json.loads((out/'public_contact_summary.json').read_text()),
                       'public_split_by_kind':{k:dict(v) for k,v in split_by_kind.items()}},
              'examples':examples,'random_seed':20261110}
    result['files_changed']=sorted(set(subprocess.check_output(['git','diff','--name-only'],cwd=ROOT,text=True).splitlines()
                                      +subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=ROOT,text=True).splitlines()))
    tests=(ROOT/'data/private/build_workspace/output/tests.log').read_text()
    match=re.search(r'Ran (\d+) tests in ([0-9.]+)s.*?\nOK',tests,re.S)
    result['tests']={'unittest':{'passed':int(match[1]),'seconds':float(match[2])} if match else 'see output/tests.log'}
    leak_path=folder/'leak_audit.json'
    if leak_path.exists():
        audit=json.loads(leak_path.read_text())
        result['tests']['public_contact_audit']={k:v for k,v in audit.items() if k!='examples'}
    browser_log=folder/'browser_smoke.log'
    if browser_log.exists():
        result['tests']['browser_smoke']='Blocked: headless Chromium startup denied setsockopt (sandbox EPERM); real GPU and interactions unverified.'
    verification=folder/'unlock_verification.json'
    if verification.exists():
        checked=json.loads(verification.read_text())
        if checked.get('html_sha256')==hashlib.sha256(html.encode()).hexdigest():
            result['tests']['unlock_retention_and_hash_stability']=checked
    result['limitations']=['Unresolved/ambiguous ownership remains hashed. Organisation proxies with mixed or conflicting company domains stay separate.',
                           'Raw index counts grow because additional lookup-only identities are retained. No original contact nodes were removed.',
                           'The existing site GDPR inbox is an explicitly configured service contact outside PUBLIC_REVEAL.']
    _write_private(folder/'report.json',json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    lines=['# Local contact review — 2026-11-10','Contains real contact values; keep under data/private.', '',
           '## Counts, files and checks', '', '```json',
           json.dumps({k:v for k,v in result.items() if k not in {'examples','resolution'}},ensure_ascii=False,indent=2), '```']
    for group in ('organisation','person','ocr_variant','hashed'):
        rows_=examples[group]
        lines += ['', '## '+group+' — '+str(len(rows_))+' seeded random examples', '', '```json',
                  json.dumps(rows_,ensure_ascii=False,indent=2), '```']
    lines += ['', 'Additional per-rule samples are retained in report.json. Seed: 20261110.']
    _write_private(folder/'report.md','\n'.join(lines)+'\n')
    print(folder/'report.md')
    print(json.dumps(result['after'],indent=2))


if __name__ == '__main__':
    main()
