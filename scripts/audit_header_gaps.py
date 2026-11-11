"""Private, bounded A5 gap coverage and source review packet; never runs a build.

Replay reports come from report_header_merges.py --resolve --review. Coverage is
mechanical affected-finding coverage, not a claim that every suggested fix is
correct. Source packets require a separate human judgment. Names stay private.
"""
import argparse
import json
import random
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import corpus
from build_communications import ROOT
from header_text import header_junk_reason
from private_contacts import write_private
from report_header_merges import object_items


def graph_id(value):
    return 'kg:' + str(value) if str(value).isdigit() else str(value)


def root(identity, mapping):
    seen = set()
    while identity in mapping and identity not in seen:
        seen.add(identity)
        identity = mapping[identity]['target']
    return identity


def cause(finding):
    """Stable, ordered root-cause buckets, retaining original classes in reports."""
    kind = finding['class'].lower()
    labels = finding.get('labels', [])
    if any(header_junk_reason(label) for label in labels):
        return 'header_greeting_form_role_syntax'
    if re.search(r'two.people|two.person|couple|compound|combined|conjunction', kind):
        return 'compound_or_multiple_people'
    if finding['theme'] in ('wrong_type', 'places', 'email_website') or re.search(r'org|company|website|email|place|domain', kind):
        return 'type_or_nonperson_identity'
    if re.search(r'first.*name|surname.only|surname_only|initial|handle|trunc|fragment.*name', kind):
        return 'partial_names_and_truncation'
    if re.search(r'prefix|suffix|noise|title|credential|leading|trailing|punct', kind):
        return 'decoration_or_header_tail'
    if finding['theme'] in ('junk_fragments', 'boilerplate_stub'):
        return 'body_signature_or_uncertain_fragment'
    return 'full_name_spelling_order_or_cross_layer'


def coverage(gaps, snapshot, before, after, active):
    live = {graph_id(n['id']): n for n in snapshot}
    old, new = before['resolved_map'], after['resolved_map']
    repaired = set(after.get('label_repairs', {}))
    result, totals = [], defaultdict(Counter)
    for finding in gaps:
        nodes = []
        for n in finding['nodes']:
            i = graph_id(n.get('current_id'))
            if i not in live or n['status'] == 'resolved':
                continue
            label = live[i].get('real') or live[i].get('rec', {}).get('name') or live[i]['label']
            syntax = header_junk_reason(label)
            merged = root(i, new) != root(i, old)
            # Later-layer-only spellings cannot be hidden by communications.
            quality_change = bool(syntax and i in active and live[i].get('rec', {}).get('label_quality') != 'not_a_name')
            nodes.append(dict(id=i, label=label, in_communications=i in active,
                              syntax=syntax, new_merge=merged, label_repair=i in repaired,
                              quality_change=quality_change, target=root(i, new)))
        bucket = cause(finding)
        counts = totals[bucket]
        counts['findings'] += 1
        touched = any(n['new_merge'] or n['label_repair'] or n['quality_change'] for n in nodes)
        counts['affected'] += touched
        counts['with_communication_node'] += any(n['in_communications'] for n in nodes)
        counts['with_later_layer_only_node'] += any(not n['in_communications'] and not n['id'].startswith('kg:') for n in nodes)
        result.append(dict(finding=finding['finding'], cause=bucket, original_class=finding['class'],
                           affected=touched, nodes=nodes))
    return dict(by_cause=dict(totals), findings=result)


def source_packet(rows, labels, lookup, db, max_docs=2):
    """Indexed evidence reads only; two documents, two pages per identity.

    For already merged spellings, try a bounded number of their raw members.
    No whole graph or whole contact lookup is loaded.
    """
    needed = {i for row in rows for i in (row['member'], row['target'])}
    aliases = defaultdict(list)
    for member, canonical in db.execute('SELECT member_id,canonical_id FROM identity_map'):
        if canonical in needed and len(aliases[canonical]) < 12:
            aliases[canonical].append(member)
    with corpus.connect() as pages:
        pages.execute('PRAGMA cache_size=-8000')
        for row in rows:
            row['label'] = labels.get(row['member'], lookup.get(row['member'], {}).get('value', ''))
            row['target_label'] = labels.get(row['target'], lookup.get(row['target'], {}).get('value', ''))
            row['raw_spellings'] = lookup.get(row['member'], {}).get('raw_spellings', [])
            row['sources'] = []
            docs = []
            # Prefer the exact member's own records. Canonical evidence can
            # otherwise select a page naming only its larger cluster.
            for i in [row['member'], *aliases[row['member']], row['target'], *aliases[row['target']]]:
                for column in ('source_id', 'target_id'):
                    for doc, kind in db.execute(f'SELECT efta_number,relationship_type FROM evidence WHERE {column}=? LIMIT 3', (i,)):
                        if doc and doc not in docs:
                            docs.append(doc)
                if len(docs) >= max_docs:
                    break
            for doc in docs[:max_docs]:
                for page, text in pages.execute('SELECT page,substr(text,1,200000) FROM pages WHERE doc_id=? ORDER BY page LIMIT 2', (doc,)):
                    lines = (text or '').splitlines()
                    keys = [re.sub(r'\W', '', x.casefold()) for x in (row['label'], row['target_label']) if x]
                    hits = [n for n, line in enumerate(lines) if any(k in re.sub(r'\W', '', line.casefold()) for k in keys)]
                    selected = set(range(min(12, len(lines))))
                    for n in hits[:8]:
                        selected.update(range(max(0, n - 2), min(len(lines), n + 4)))
                    row['sources'].append(dict(doc=doc, page=page, lines=[f'{n+1}: {lines[n]}' for n in sorted(selected)]))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    private = ROOT / 'data/private/audit'
    parser.add_argument('--before', type=Path, default=private / 'a5_baseline.json')
    parser.add_argument('--after', type=Path, default=private / 'a5_after.json')
    parser.add_argument('--gaps', type=Path, default=private / 'gap_A_people.json')
    parser.add_argument('--snapshot', type=Path, default=private / 'viewer_nodes.json')
    parser.add_argument('--out', type=Path, default=private / 'a5_evaluation.json')
    parser.add_argument('--sources', action='store_true')
    args = parser.parse_args()
    if not args.out.resolve().is_relative_to((ROOT / 'data/private').resolve()):
        raise SystemExit('Private audit output must stay under data/private/')
    before, after, gaps, snapshot = [json.loads(p.read_text()) for p in (args.before, args.after, args.gaps, args.snapshot)]
    labels = {graph_id(n['id']): n.get('real') or n.get('rec', {}).get('name') or n['label'] for n in snapshot}
    prior = {}
    for i, entry in object_items(ROOT / 'output/identity_map.json'):
        prior[i] = dict(target=graph_id(entry['canonical']))
    candidates = []
    for i, entry in after['resolved_map'].items():
        if i not in labels or not entry['rule'].startswith('header_') or root(i, prior) == root(entry['target'], prior):
            continue
        candidates.append(dict(member=i, **entry, code_delta=root(i, before['resolved_map']) != root(i, after['resolved_map'])))
    # Include every code delta first, then a seeded sample of other replay
    # proposals new relative to the built map. Report those populations apart.
    changed = [r for r in candidates if r['code_delta']]
    rest = [r for r in candidates if not r['code_delta']]
    sample = changed + random.Random(51).sample(rest, min(max(0, 60 - len(changed)), len(rest)))
    with sqlite3.connect(f'file:{ROOT / "output/communication_graph.db"}?mode=ro', uri=True) as db:
        db.execute('PRAGMA cache_size=-8000')
        active = {i for i, in db.execute('SELECT id FROM nodes')}
        report = coverage(gaps, snapshot, before, after, active)
        if args.sources:
            needed = {i for r in sample for i in (r['member'], r['target'])}
            lookup = {i: e for i, e in object_items(ROOT / 'data/private/contact_lookup.json') if i in needed}
            sample = source_packet(sample, labels, lookup, db)
    quality = Counter(header_junk_reason(label) for i, label in labels.items() if i in active and header_junk_reason(label))
    report['summary'] = dict(gap_findings=len(gaps), affected_findings=sum(f['affected'] for f in report['findings']),
                             snapshot_syntax_rules=dict(quality), code_delta_merges=len(changed),
                             replay_merges_new_vs_built=len(candidates), source_review_sample=len(sample),
                             review_populations=dict(Counter('code_delta' if r['code_delta'] else 'replay_only' for r in sample)))
    report['merge_review'] = sample
    report['limits'] = ['Snapshot replay uses fewer frozen references than a full build; candidate counts are predictions.',
                        'Affected-finding coverage is an upper bound on recall, not verified resolution of every requested action.',
                        'Later entity-layer-only nodes need the corresponding layer changes; communications cannot relabel them.',
                        'Source packets are unjudged until a human supplies verdicts; code-delta and replay-only merges are distinct.']
    write_private(args.out, json.dumps(report, ensure_ascii=False, indent=1) + '\n')
    print(json.dumps(dict(summary=report['summary'], by_cause=report['by_cause']), indent=1))


if __name__ == '__main__':
    main()
