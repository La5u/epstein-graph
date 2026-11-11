"""Audit and report the isolated, fixed 600-document entity pilot."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sqlite3

import corpus
from entity_resolution import read_nodes

ROOT = Path(__file__).resolve().parents[1]


def report(pilot, out, tests_log):
    existing = read_nodes(out)
    selection = json.loads((pilot / 'sample_selection.json').read_text())
    sample = set(json.loads((pilot / 'sample_docs.json').read_text()))
    source = corpus.connect(corpus.PATH)
    source.execute('CREATE TEMP TABLE sample(id PRIMARY KEY,cohort)')
    for cohort in ('range', 'random'):
        source.executemany('INSERT INTO sample VALUES (?,?)', ((d, cohort) for d in selection[cohort + '_documents']))
    assert sample == {r[0] for r in source.execute('SELECT id FROM sample')}
    source.execute('ATTACH DATABASE ? AS pilot', (str(pilot / 'entity_mentions.db'),))
    expected = source.execute('SELECT count(*) FROM pages WHERE doc_id IN (SELECT id FROM sample)').fetchone()[0]
    assert source.execute('SELECT count(*) FROM pilot.done').fetchone()[0] == expected
    assert source.execute('SELECT count(*) FROM pages JOIN pilot.done USING(page_id) WHERE doc_id IN (SELECT id FROM sample)').fetchone()[0] == expected
    assert not source.execute('SELECT 1 FROM pilot.mentions WHERE doc_id NOT IN (SELECT id FROM sample) LIMIT 1').fetchone()
    cohorts = {c: dict(documents=d, pages=p, nonempty_pages=n, characters=ch)
               for c, d, p, n, ch in source.execute('''SELECT cohort,count(DISTINCT doc_id),count(*),
               sum(length(coalesce(text,''))>0),sum(length(coalesce(text,''))) FROM pages JOIN sample ON sample.id=pages.doc_id WHERE doc_id IN (SELECT id FROM sample) GROUP BY cohort''')}
    metrics = json.loads((pilot / 'entity_mentions.metrics.json').read_text())
    assert metrics['pages'] == expected and metrics['sample_documents'] == len(sample) == 600
    assert metrics['characters'] == sum(c['characters'] for c in cohorts.values())
    source.close()
    db = sqlite3.connect(pilot / 'entity_graph.db')
    db.execute('ATTACH DATABASE ? AS extracted', (str(pilot / 'entity_mentions.db'),))
    nodes = json.loads((pilot / 'entity_nodes.json').read_text())
    created = [n for n in nodes if n['id'] not in existing]
    visible = [n for n in created if n.get('label_quality') != 'not_a_name']
    counts = defaultdict(Counter)
    for n in nodes:
        c = counts[n['entity_type']]
        c['total'] += 1
        hidden = n.get('label_quality') == 'not_a_name'
        c['hidden'] += hidden
        if n['id'] not in existing:
            c['created'] += 1
            c['created_hidden' if hidden else 'created_visible'] += 1
    folds = {kind: {'keys': keys, 'existing_nodes': targets} for kind, keys, targets in
             db.execute('SELECT target_type,count(*),count(DISTINCT entity_id) FROM folds GROUP BY target_type')}
    candidates = [dict(span=span, target_id=identity, target_name=existing[identity]['name'],
                       target_type=kind, reason=reason, documents=docs)
                  for span, identity, kind, reason, docs in db.execute('''SELECT m.spelling,f.entity_id,f.target_type,f.reason,count(DISTINCT m.doc_id)
                    FROM extracted.mentions m JOIN folds f USING(key) GROUP BY m.spelling,f.entity_id
                    ORDER BY count(DISTINCT m.doc_id) DESC,m.spelling''')]
    examples = []
    def take(row):
        if row not in examples and len(examples) < 40:
            examples.append(row)
    for span in ('Southern Financial', 'Deutsche Bank Trust Company Americas', 'Deutsche Bank Securities Inc',
                 'Bank Trust Company Americas', 'Chase', 'JPM', 'JPMC', 'J.P. Morgan', 'BofA'):
        for row in candidates:
            if row['span'].casefold() == span.casefold():
                take(row)
                break
    for kind in sorted(folds):
        for row in [r for r in candidates if r['target_type'] == kind][:3]:
            take(row)
    for row in candidates:
        take(row)
    assert len(examples) == 40
    for span, target in [('Southern Financial','company_7b09f280b14a15e9'),
                         ('Deutsche Bank Securities Inc','company_ad3bd5febadbf5af'),
                         ('Deutsche Bank Trust Company Americas','company_8d1bbd8128069840'),
                         ('Bank Trust Company Americas','company_8d1bbd8128069840'), ('Chase',498)]:
        matches = [r for r in candidates if r['span'].casefold() == span.casefold()]
        assert matches and all(r['target_id'] == target for r in matches), (span, matches)
    assert any(n['name'] == 'New York' and n['entity_type'] == 'location' for n in nodes)
    assert not any(r['span'] == 'New York' and r['target_type'] != 'location' for r in candidates)
    bad = {'NANCY','DOCTYPE','RM0082289 Daily Deposit Report','jeevacation®gmail.com',
           'jeevacationegmail.com','jeevacationggmail.com','New York City','United States of America',
           'Southern Financial','Deutsche Bank Securities Inc','Deutsche Bank Trust Company Americas','Bank Trust Company Americas'}
    assert not bad & {n['name'] for n in visible}
    # The public mention DB must carry corrected people and email values only as
    # HMAC labels. Gazetteer/raw organization spellings remain reviewable.
    assert not db.execute("SELECT 1 FROM extracted.mentions WHERE kind IN ('PERSON','person_weak','email') AND spelling NOT LIKE 'HeaderLabel_%' AND spelling NOT LIKE 'Email_%' LIMIT 1").fetchone()
    private = sqlite3.connect(f'file:{ROOT / "data/private/entity_pilot_spellings.db"}?mode=ro', uri=True)
    email_spellings = ['jeevacation®gmail.com', 'jeevacationegmail.com', 'jeevacationggmail.com']
    email_targets = set()
    for value in email_spellings:
        keys = [r[0] for r in private.execute('SELECT key FROM spellings WHERE spelling=?', (value,))]
        folded = [db.execute("SELECT entity_id FROM folds WHERE key=? AND target_type='email'", (key,)).fetchone() for key in keys]
        assert any(folded), 'Requested OCR email variant must reuse an existing email node'
        email_targets.update(row[0] for row in folded if row)
    assert len(email_targets) == 1
    for value in ['Bradley Ghasil', 'Brigid Mguhat', 'NANCY']:
        keys = [r[0] for r in private.execute('SELECT key FROM spellings WHERE spelling=?', (value,))]
        assert any(db.execute("SELECT 1 FROM extracted.mentions WHERE key=? AND kind IN ('PERSON','person_weak')", (key,)).fetchone() for key in keys)
    private.close()
    tests = tests_log.read_text()
    match = re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$', tests)
    assert match, 'Full tests must pass before reporting'
    stats = json.loads((pilot / 'entity_summary.json').read_text())
    graph_mentions, graph_documents = db.execute('SELECT count(*),count(DISTINCT efta_number) FROM mentions').fetchone()
    rows, occurrences, documents = db.execute('SELECT count(*),sum(n),count(DISTINCT doc_id) FROM extracted.mentions').fetchone()
    kinds = sorted(counts)
    visible.sort(key=lambda n: (n['entity_type'], -n['metadata']['document_count'], n['name']))
    noise = [
        'Deutsche Bank Wealth Management has no matching existing name, alias or company key in the current company/KG/graph inputs. It remains a separate unverified organization; regression coverage verifies folding when a matching node exists.',
        'NER still produces all-caps surnames and generic phrases such as WANEK and Kind Regards. The given-name rule is deliberately limited to its configured list; it is not a general person classifier.',
        'Short domain fragments such as ail.com, by.net and the.net can still be OCR artifacts. Unresolved email spans are hidden email evidence, never visible organizations or websites.',
        'Title Case labels, OCR-damaged organizations and street/facility fragments remain unverified. Repetition across bank records and near-duplicates does not establish identity or relevance.',
        'Existing duplicate company identities are retained. Bare equivalent keys prefer the KG identity or the most evidenced company; exact legal names prefer that existing company node. Unrelated ambiguous aliases stay unresolved.',
    ]
    result = dict(selection=selection, cohorts=cohorts, extraction=metrics, mention_rows=rows, occurrences=occurrences,
                  documents_with_mentions=documents, graph_mentions=graph_mentions, graph_documents=graph_documents,
                  aggregation=stats, created=len(created), created_visible=len(visible), created_hidden=len(created)-len(visible),
                  hidden_total=sum(c['hidden'] for c in counts.values()), counts_by_type={k:dict(counts[k]) for k in kinds},
                  folds_by_target_type=folds, fold_examples=examples,
                  hidden_reasons=dict(Counter(n.get('label_quality_reason','existing_hidden') for n in nodes if n.get('label_quality')=='not_a_name')),
                  visible_created_nodes=visible, remaining_noise=noise,
                  tests=dict(count=int(match[1]),seconds=float(match[2]),log=str(tests_log.relative_to(out))))
    (out / 'entity_pilot_report.json').write_text(json.dumps(result,indent=2)+'\n')
    lines = ['# Entity layer: isolated 600-document pilot', '',
        f"300 documents from EFTA01420000–EFTA01430000 ({selection['range_available']:,} available), plus 300 seeded random documents outside that range (seed {selection['seed']}). The original `entity_pilot/sample_docs.json` was reused. All **600 documents / {expected:,} pages** were processed; page coverage and exclusion of outside documents were checked against the corpus.", '',
        '| Sample | Documents | Pages | Nonempty pages | Characters |', '| --- | ---: | ---: | ---: | ---: |']
    for cohort,c in cohorts.items():
        lines.append(f"| {cohort} | {c['documents']:,} | {c['pages']:,} | {c['nonempty_pages']:,} | {c['characters']:,} |")
    lines += ['', f"Extraction: **{metrics['characters']:,} characters in {metrics['seconds']:.3f} s = {metrics['chars_per_second']:,.2f} chars/s**, 3 workers, measured with `perf_counter` including startup and database writes. This is pilot throughput.", '',
        f"{rows:,} mention rows / {occurrences:,} occurrences across {documents:,} documents. Graph: {stats['nodes']:,} nodes, {graph_mentions:,} entity/document mentions across {graph_documents:,} documents, {stats['pairs']:,} complete co-mention pairs and {stats['viewer_edges']:,} viewer edges.", '',
        f"**Created: {len(created):,} nodes; {len(visible):,} visible, {len(created)-len(visible):,} hidden. Total hidden, including reused nodes: {result['hidden_total']:,}.**", '',
        '## Folding into existing nodes', '',
        f"**{sum(c['keys'] for c in folds.values()):,} extraction keys fold into {sum(c['existing_nodes'] for c in folds.values()):,} distinct existing nodes.** Counts below measure canonical extraction keys, so gazetteer aliases such as Chase/JPM/JPMC share a key. The previous pilot folded 216 keys into 196 existing nodes.", '',
        '| Existing target type | Folded keys | Distinct existing nodes |', '| --- | ---: | ---: |']
    for kind,c in sorted(folds.items()):
        lines.append(f"| {kind} | {c['keys']:,} | {c['existing_nodes']:,} |")
    lines += ['', 'Names, original names and aliases are loaded from normalized KG, company, place, contact, communication and combined graph inputs. A node’s own name wins over another node’s alias: Securities and Trust Americas reuse their company nodes even though the KG also lists these legal names under Deutsche Bank. Bare Southern Financial reuses the most evidenced equivalent existing company, Southern Financial LLC; explicit Inc. still reuses its own legal node. No existing nodes were merged or renamed.', '',
        'Left-truncated tails require at least three matching words plus the actual longer name on the same page. Extraction recovers exact full-name anchors when NER misses them. OCR email variants use the communication-layer mailbox repair and retained email identities, otherwise remain hidden. Raw email and personal spellings stay private.', '',
        '### 40 span → existing node examples', '',
        '| Span | Existing node | Target type | Rule | Documents |', '| --- | --- | --- | --- | ---: |']
    def cell(value):
        return str(value).replace('|','\\|').replace('\n',' ')
    for row in examples:
        lines.append(f"| {cell(row['span'])} | {cell(row['target_name'])} (`{row['target_id']}`) | {row['target_type']} | {row['reason']} | {row['documents']:,} |")
    lines += ['', '## Created nodes', '', '| Type | Total nodes | Created | Created visible | Created hidden | Total hidden |', '| --- | ---: | ---: | ---: | ---: | ---: |']
    for kind in kinds:
        c=counts[kind]
        lines.append('| '+kind+' | '+' | '.join(f'{c[k]:,}' for k in ('total','created','created_visible','created_hidden','hidden'))+' |')
    lines += ['', f"Validation: **all {match[1]} tests passed** in {match[2]} s (`{tests_log.name}`). Additional pilot assertions check full sample/page coverage, the five reported bank/company folds, pseudonymous person/email storage, and removal of the named noise/duplicate variants from visible created nodes.", '',
        'The isolated extraction/graph databases, JSON and logs are in `entity_pilot/`; private spellings are in `data/private/entity_pilot_spellings.db`. The real resumable extraction databases and main graph outputs were not rebuilt.', '',
        '## Remaining noise', '']
    lines += ['- '+n for n in noise]
    lines += ['', '## All visible created nodes', '',
        'Complete list, ordered by type, document count and label. Reused and `not_a_name` nodes are excluded. Person labels remain pseudonymous. Co-mention evidence is a reading lead, not evidence of a relationship or wrongdoing.']
    for kind in kinds:
        subset=[n for n in visible if n['entity_type']==kind]
        if not subset:
            continue
        lines += ['', f"### {kind}: {len(subset):,} visible created nodes", '', '| ID | Label | Documents | Extraction kind |', '| --- | --- | ---: | --- |']
        for n in subset:
            lines.append(f"| {n['id']} | {cell(n['name'])} | {n['metadata']['document_count']:,} | {n['metadata']['entity_mention_kind']} |")
    (out / 'entity_pilot_report.md').write_text('\n'.join(lines)+'\n')
    db.close()
    print(json.dumps({k:result[k] for k in ('aggregation','created','created_visible','created_hidden','folds_by_target_type','tests')},indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=ROOT/'output')
    args=p.parse_args()
    report(args.output/'entity_pilot',args.output,args.output/'entity_pilot_tests.log')


if __name__ == '__main__':
    main()
