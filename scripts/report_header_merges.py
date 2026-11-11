"""Private, bounded-memory audit of CURRENT viewer duplicates; never runs a build.

  .venv/bin/python -B scripts/report_header_merges.py [--resolve] [--review]

Refresh viewer_nodes.json after rebuilding, then rerun to see remaining groups.
--resolve replays resolution against the existing read-only communications DB
using snapshot names and streamed contact values (not a full communications run).
--review adds ranked structural review candidates (generic spelling skipped);
no labels are sent to a hosted model.
Only aggregate counts go to stdout; names/addresses stay under data/private/.
"""
import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from build_communications import (ROOT, FrequentNameFamilies, PersonMatcher, address_family_merges,
                                  load_identity_merges, never_merge_pairs, node_review, non_mailbox_reason,
                                  resolve_identities, resolved_header_compounds)
from private_contacts import cleaned_header_labels
from header_context_resolution import ContextNames


def object_items(path, chunk_size=65536):
    """Stream a JSON object, including large/multiline values, without ijson."""
    decoder = json.JSONDecoder()
    with path.open() as stream:
        buffer, position, eof = '', 0, False
        def token():
            nonlocal buffer, position, eof
            while True:
                while position < len(buffer) and buffer[position] in ' \n\r\t,:{':
                    position += 1
                if position < len(buffer) and buffer[position] == '}':
                    return None
                try:
                    value, end = decoder.raw_decode(buffer, position)
                    position = end
                    return value
                except ValueError:
                    if eof:
                        raise
                    buffer = buffer[position:] + stream.read(chunk_size)
                    position = 0
                    eof = not buffer or stream.tell() == path.stat().st_size
        while (key := token()) is not None:
            # The value may itself start with '{'; do not strip its opening brace.
            while True:
                while position < len(buffer) and buffer[position] in ' \n\r\t:':
                    position += 1
                try:
                    value, end = decoder.raw_decode(buffer, position)
                    position = end
                    break
                except ValueError:
                    if eof:
                        raise
                    buffer = buffer[position:] + stream.read(chunk_size)
                    position = 0
                    eof = not buffer or stream.tell() == path.stat().st_size
            yield key, value


def grouped(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row['reason'], row['target']].append(row)
    return [dict(reason=reason, target=target, members=items)
            for (reason, target), items in sorted(groups.items())]


def report(args):
    snapshot = json.loads(args.snapshot.read_text())  # ~8 MB, never graph_entities.json
    labels, people, names, nodes, counts = {}, {}, {}, {}, Counter()
    for n in snapshot:
        i = f"kg:{n['id']}" if isinstance(n['id'], int) else n['id']
        label = n.get('real') or n['label']
        # KG status badges are presentation; use the underlying recorded name.
        if i.startswith('kg:'):
            label = n.get('rec', {}).get('name') or label
        labels[i] = label
        kind = n.get('rec', {}).get('entity_type', n['type'])
        if n['type'] == 'contact' and '@' in label:
            kind = 'email'
        if n['type'] in ('person', 'participant') and not n.get('rec', {}).get('suspected_parse_artifact'):
            (people if i.startswith('kg:') else names)[i] = label
        nodes[i] = {**n.get('rec', {}), 'entity_type': kind}
        counts[i] = n.get('rec', {}).get('record_count', 0)
    emails = {}
    for i, entry in object_items(ROOT / 'data/private/contact_lookup.json'):
        if entry.get('kind') == 'email':
            emails[i] = entry['value']
            counts[i] = entry.get('occurrences', 0)
        elif i in names:
            counts[i] = entry.get('occurrences', 0)
    all_names = {**people, **names}
    families = FrequentNameFamilies(all_names, counts, node_review().get('participants', {}))
    proposals = []
    for i in names:
        if result := families.header_variants.match(i):
            target, reason = result
            target = families.aliases.get(target, (target,))[0]
            proposals.append(dict(member=i, label=all_names[i], target=target,
                                  target_label=all_names[target], reason=reason))
    mail_proposals = [dict(member=i, label=emails[i], target=target, target_label=emails[target], reason=reason)
                      for i, (target, reason) in address_family_merges(emails, counts).items()]
    transport = [dict(id=i, value=value, reason=reason) for i, value in emails.items()
                 if (reason := non_mailbox_reason(value))]
    summary = dict(snapshot_nodes=len(snapshot), header_candidates=dict(Counter(r['reason'] for r in proposals)),
                   email_candidates_all_contacts=dict(Counter(r['reason'] for r in mail_proposals)),
                   non_mailboxes_all_contacts=dict(Counter(r['reason'] for r in transport)),
                   non_mailboxes_in_snapshot=dict(Counter(r['reason'] for r in transport if r['id'] in labels)))
    summary['context_thresholds'] = dict(min_correspondents=ContextNames.MIN_CORRESPONDENTS,
                                         hub_above_neighbors=ContextNames.MAX_HUB_NEIGHBORS,
                                         max_document_people=ContextNames.MAX_DOC_PEOPLE,
                                         min_neighbor_overlap=.6, min_mailbox_agreement=.9)
    result = dict(summary=summary, header_groups=grouped(proposals), email_groups=grouped(mail_proposals),
                  non_mailboxes=transport)
    stripped = cleaned_header_labels({i: dict(kind='participant', value=label) for i, label in names.items()},
                                    json.loads((ROOT / 'config/survivors.json').read_text())['names'])
    result['label_repairs'] = {i: dict(label=labels[i], **repair) for i, repair in stripped.items()}
    summary['label_repairs'] = dict(Counter(r['rule'] for r in stripped.values()))
    if args.resolve:
        with closing(sqlite3.connect(f'file:{ROOT / "output/communication_graph.db"}?mode=ro', uri=True)) as db:
            db.execute('PRAGMA cache_size=-16000')
            existing = {row[0] for row in db.execute('SELECT id FROM nodes')}
            # Names introduced by later layers cannot be changed by communications.
            active = {i: name for i, name in names.items() if i in existing}
            missing = [p for p in proposals if p['member'] not in existing]
            diagnostics = []
            support_aliases = dict(db.execute('SELECT member_id,canonical_id FROM identity_map'))
            merges = resolve_identities(db, emails, load_identity_merges(), never_merge_pairs(),
                                        participant_labels=active, person_names=people,
                                        matcher=PersonMatcher([dict(id=int(i[3:]), name=name, entity_type='person')
                                                               for i, name in people.items()]),
                                        diagnostics=diagnostics, support_aliases=support_aliases, context_corpus=True)
        measured = [dict(member=i, target=t, reason=r, confidence=c,
                         label=labels.get(i, emails.get(i, '')), target_label=labels.get(t, emails.get(t, '')))
                    for i, (t, r, c) in merges.items()
                    if r.startswith(('header_name_ocr_family:', 'ocr_address_family:', 'header_context:'))
                    or r in ('same_gmail_mailbox', 'ocr_variant_of_more_frequent_address', 'ocr_domain_typo', 'header_name_mangled_address', 'header_name_is_address', 'header_name_is_misread_address', 'header_name_address_tail')]
        summary['resolver_merges'] = dict(Counter(r['reason'] for r in measured))
        result['resolved_map'] = {i: dict(target=t, rule=r) for i, (t, r, _) in merges.items()}
        def final(i):
            return merges.get(i, (i,))[0]
        compounds = resolved_header_compounds(all_names, final)
        result['compounds'] = [dict(id=i, label=labels[i], targets=sorted(people),
                                   target_labels=[labels.get(p, p) for p in sorted(people)])
                               for i, people in sorted(compounds.items())]
        summary['compound_labels'] = len(compounds)
        summary['compound_constituent_links'] = sum(map(len, compounds.values()))
        prior = {}
        prior_path = ROOT / 'output/identity_map.json'
        if prior_path.exists():
            for i, entry in object_items(prior_path):
                target = entry['canonical']
                prior[i] = f'kg:{target}' if isinstance(target, int) else target
            def old_root(i):
                seen = set()
                while i in prior and i not in seen:
                    seen.add(i)
                    i = prior[i]
                return i
            summary['new_vs_current_identity_map'] = dict(Counter(
                r['reason'] for r in measured if old_root(r['member']) != old_root(r['target'])))
        summary['candidates_from_later_layers'] = dict(Counter(p['reason'] for p in missing))
        summary['resolver_rejections'] = dict(Counter(r['reason'] for r in diagnostics if r['reason'] != 'accepted_context'))
        result['context_evidence'] = [r for r in diagnostics if r['reason'] == 'accepted_context']
        result.update(resolver_merges=measured, later_layer_candidates=missing, resolver_rejections=diagnostics)
    if args.review:
        from luna_review import candidate_pairs
        # This audit concerns header names/mailboxes; company review remains
        # available through luna_review.py merges. Avoid quadratic company noise.
        review_nodes = {i: n for i, n in nodes.items() if n['entity_type'] in ('person', 'participant', 'email')}
        pairs = candidate_pairs(review_nodes, labels, defaultdict(Counter), include_spelling=False)
        summary['review_scope'] = 'structural person/header/mailbox pairs; generic spelling skipped'
        summary['review_candidates'] = dict(Counter(why for _, _, why in pairs))
        result['review_candidates'] = [dict(a=a, b=b, label_a=labels[a], label_b=labels[b], reason=why) for a, b, why in pairs]
        live = set(labels)
        reviews = {}
        for path in (ROOT / 'data/private/luna_review').glob('merges_*.json'):
            old = json.loads(path.read_text())
            keys = [key.split('|') for key in old]
            present = sum(len(pair) == 2 and all((f'kg:{i}' if i.isdigit() else i) in live for i in pair) for pair in keys)
            reviews[path.name] = dict(pairs=len(keys), endpoints_still_in_snapshot=present)
        summary['historical_review_coverage'] = reviews
    private = (ROOT / 'data/private').resolve()
    if not args.out.resolve().is_relative_to(private):
        raise SystemExit('Report contains real labels; --out must be inside data/private/')
    args.out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Create with private permissions, including during the write.
    import os
    with os.fdopen(os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(result, stream, indent=1)
        stream.write('\n')
    print(json.dumps(summary, indent=1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, default=ROOT / 'data/private/audit/viewer_nodes.json')
    parser.add_argument('--out', type=Path, default=ROOT / 'data/private/audit/header_merge_report.json')
    parser.add_argument('--resolve', action='store_true')
    parser.add_argument('--review', action='store_true')
    parser.add_argument('--min-correspondents', type=int, choices=(2, 3, 4), default=ContextNames.MIN_CORRESPONDENTS,
                        help='audit-only threshold sensitivity; production defaults remain in ContextNames')
    args = parser.parse_args()
    ContextNames.MIN_CORRESPONDENTS = args.min_correspondents
    report(args)


if __name__ == '__main__':
    main()
