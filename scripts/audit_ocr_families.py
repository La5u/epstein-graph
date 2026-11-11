"""Read-only identity-resolution audit; labels are written only under data/private.

Run after build_communications changes without rebuilding downstream graphs:
    .venv/bin/python -B scripts/audit_ocr_families.py
The private lookup supplies every participant, including lookup-only names;
saved evidence supplies their frequencies and context, beyond the viewer.
"""
import argparse
import json
import os
import random
import resource
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

import build_communications as communications
from rapidfuzz.distance import Levenshtein


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='ocr_family_merges.json', help='Filename under data/private/audit')
    parser.add_argument('--compare', help='Prior audit filename under data/private/audit')
    args = parser.parse_args()
    if Path(args.output).name != args.output:
        parser.error('--output must be a filename')
    if args.compare and Path(args.compare).name != args.compare:
        parser.error('--compare must be a filename')
    root = communications.ROOT
    previous = (json.loads((root / 'data/private/audit' / args.compare).read_text())
                if args.compare else None)
    lookup = json.loads((root / 'data/private/contact_lookup.json').read_text())
    entities = json.loads((root / 'output/normalized_entities.json').read_text())
    entity_names = {f'kg:{e["id"]}': e['name'] for e in entities}
    person_names = {f'kg:{e["id"]}': e['name'] for e in entities
                    if e['entity_type'] == 'person' and not e.get('suspected_parse_artifact')}
    with closing(sqlite3.connect(f'file:{root}/output/communication_graph.db?mode=ro', uri=True)) as db:
        db.execute('PRAGMA cache_size=-32768')
        db.execute('PRAGMA temp_store=FILE')
        counts = Counter()
        for column in ('source_id', 'target_id'):
            counts.update(dict(db.execute(f'SELECT {column},count(*) FROM evidence GROUP BY {column}')))
        node_ids = {i for i, in db.execute('SELECT id FROM nodes')}
        imported_ids = node_ids | counts.keys()
        imported_participants = {i for i in imported_ids if i.startswith('participant_')}
        missing = imported_participants - lookup.keys()
        if missing:
            raise ValueError(f'{len(missing)} evidence participants absent from private lookup')
        ids = {i for i in lookup if i.startswith('participant_')}
        labels = {i: communications.name_key(lookup[i]['value']) for i in sorted(ids)}
        spellings = {i: lookup[i].get('raw_spellings', []) for i in labels}
        emails = {i: v['value'] for i, v in lookup.items() if i.startswith('email_') and i in imported_ids}
        baseline = dict(db.execute('SELECT member_id,canonical_id FROM identity_map'))
        curated = communications.load_identity_merges()
        reviewed = communications.node_review().get('participants', {})
        candidates = communications.FrequentNameFamilies({**person_names, **labels}, counts, reviewed, db, spellings)
        rejections = []
        merges = communications.resolve_identities(
            db, emails, curated, communications.never_merge_pairs(),
            labels, communications.PersonMatcher(entities), entity_names, person_names, spellings,
            families=candidates, diagnostics=rejections)

    names = {**entity_names, **{i: v['value'] for i, v in lookup.items()}}
    family = []
    changed = []
    nonperson = 0
    for member, (canonical, rule, _) in sorted(merges.items()):
        if not rule.startswith('header_name_ocr_family:'):
            continue
        if canonical.startswith(('company_', 'email_')) or canonical not in names:
            nonperson += 1
            continue
        a, b = names[member], names[canonical]
        wa, wb = communications.family_name_words(a), communications.family_name_words(b)
        scored = candidates.candidates(member)
        best_distance = min(s[0] for s in scored.values())
        matched = [i for i, s in scored.items() if s[0] == best_distance
                   and merges.get(i, (i,))[0] == canonical]
        reference = max(matched, key=lambda i: (counts[i], i)) if matched else canonical
        forms = [wa, wa[::-1]] if len(wa) == 2 else [wa]
        # Candidates retain alias spellings but return the clean target. Recover
        # that literal spelling so an OCR correction plus a tail is auditable.
        if not any(reference in candidates.references
                   and (score := candidates.score(w, reference, member)) is not None
                   and score[0] <= best_distance for w in forms):
            aliases = {i for w in forms for i in candidates.by_first.get(communications.family_ocr_key(w[0]), ())
                       if i in candidates.aliases and merges.get(candidates.aliases[i][0], (candidates.aliases[i][0],))[0] == canonical
                       and (score := candidates.score(w, i, member)) is not None
                       and score[0] <= best_distance}
            if aliases:
                reference = min(aliases, key=lambda i: (min(candidates.score(w, i, member)
                    for w in forms if candidates.score(w, i, member) is not None), -counts[i], i))
        valid = [w for w in forms if reference in candidates.references and candidates.score(w, reference, member) is not None]
        if valid:
            wa = min(valid, key=lambda w: candidates.score(w, reference, member))
        def surnames(words):
            return {communications.family_ocr_key(''.join(rest)) for rest in
                    (words[1:], tuple(w for w in words[1:] if len(w) > 1))}
        distance = min(Levenshtein.distance(x, y) for x in surnames(wa) for y in surnames(wb))
        reference_distance = min(Levenshtein.distance(x, y) for x in surnames(wa)
                                 for y in surnames(communications.family_name_words(names[reference])))
        reference_target_distance = min(Levenshtein.distance(x, y)
            for x in surnames(communications.family_name_words(names[reference])) for y in surnames(wb))
        direct = candidates.score(wa, canonical, member) if canonical in candidates.references else None
        literal = candidates.score(wa, reference, member) if reference in candidates.references else None
        alias = candidates.aliases.get(reference)
        validated_alias = (alias is not None and merges.get(alias[0], (alias[0],))[0] == canonical
                           and candidates.score(wa, reference, member) is not None)
        row = dict(member=member, canonical=canonical, variant=a, reference=b, rule=rule,
                   variant_records=counts[member], reference_records=counts[canonical],
                   matched_reference=names[reference], matched_reference_id=reference,
                   matched_reference_records=counts[reference], matched_reference_distance=reference_distance,
                   reference_target_surname_distance=reference_target_distance,
                   surname_distance=distance, previous_canonical=baseline.get(member, member),
                   shared_context=candidates.shared_context(member, canonical),
                   shared_address_records=sum(n for address, n in candidates.addresses[member].items()
                                              if address in candidates.addresses[canonical]),
                   own_address_records=sum(candidates.addresses[member].values()),
                   structured_explanation=any(score and score[1] in
                       ('truncated_context', 'short_tail', 'long_tail', 'glued_tail', 'stroke_suffix',
                        'self_description', 'anchored_ligature')
                       for score in (direct, literal if reference_target_distance <= 2 else None)),
                   validated_reference_alias=validated_alias)
        family.append(row)
        if baseline.get(member, member) != canonical:
            changed.append(row)
    suspicious = [row for row in family if row['surname_distance'] > 2]
    for row in suspicious:
        if row['matched_reference_distance'] <= 2 and (row['matched_reference_id'] in curated
                                                      or 'merge_into' in reviewed.get(row['matched_reference_id'], {})):
            row['distance_review'] = 'reviewed_alias'
        elif row['structured_explanation']:
            row['distance_review'] = 'structured_truncation_or_tail'
        elif row['validated_reference_alias']:
            row['distance_review'] = 'ocr_and_validated_reference_alias'
        elif (merges.get(row['matched_reference_id'], (None, None))[:2]
              == (row['canonical'], 'header_name_address_tail')):
            row['distance_review'] = 'validated_address_owner'
        else:
            row['distance_review'] = 'inspect'
    rng = random.Random(0)
    sample_pool = changed if len(changed) >= 40 else family
    sample = rng.sample(sample_pool, min(40, len(sample_pool)))
    family_counts, remaining = {}, {}
    for prefix in ('jeffrey e', 'lesley gro', 'darren ind', 'larry vis'):
        members = {i for i in ids if names[i].casefold().startswith(prefix)}
        before = {baseline.get(i, i) for i in members}
        after = {merges.get(i, (i,))[0] for i in members}
        family_counts[prefix] = dict(participants=len(members), before=len(before), after=len(after))
        remaining[prefix] = [dict(id=i, label=names.get(i, i), records=counts[i],
                                  members=sum(merges.get(m, (m,))[0] == i for m in members))
                             for i in sorted(after, key=lambda i: (-counts[i], i))]
    unexplained = [row for row in suspicious if row['distance_review'] == 'inspect']
    context_only_spelling = [row for row in family if row['rule'].endswith((':anchored_spelling', ':anchored_ligature'))
                            and not row['shared_address_records']]
    new_context_only = [row for row in context_only_spelling if previous is None or
                        previous['participant_identity_map'].get(row['member'], {}).get('canonical', row['member'])
                        != row['canonical']]
    participant_merges = {i: dict(canonical=c, rule=r) for i, (c, r, _) in merges.items() if i in ids}
    unresolved = []
    for identity in sorted(ids - merges.keys()):
        words = candidates.words[identity]
        if len(words) < 2:
            continue
        targets = []
        for target in sorted(candidates.by_first.get(communications.family_ocr_key(words[0]), ())):
            if target == identity or target in candidates.aliases:
                continue
            score = candidates.score(words, target, identity, allow_no_context=True)
            tails = [dict(tail=tail, kind=kind, veto=candidates.tail_veto(tail, target, identity))
                     for tail, kind in candidates.prefix_tails(words, target)]
            if score is not None or tails:
                targets.append(dict(id=target, name=names[target], score=score,
                                    shared_context=candidates.shared_context(identity, target), tails=tails))
        if targets or identity in candidates.references:
            unresolved.append(dict(id=identity, name=names[identity], records=counts[identity],
                                   is_reference=identity in candidates.references,
                                   alias=candidates.aliases.get(identity),
                                   candidates=candidates.candidates(identity), targets=targets))
    # Cover every frozen family, including unresolved nearest ties, rather than
    # limiting the regression sweep to the four illustrative name prefixes.
    groups = {}
    for identity in ids:
        targets = {merges.get(identity, (identity,))[0]}
        scores = candidates.candidates(identity)
        if scores:
            distance = min(s[0] for s in scores.values())
            targets.update(merges.get(i, (i,))[0] for i, s in scores.items() if s[0] == distance)
        for target in targets:
            if target != identity or target in candidates.references:
                groups.setdefault(target, set()).add(identity)
    comparison_map = ({i: row['canonical'] for i, row in previous['participant_identity_map'].items()}
                      if previous else baseline)
    all_counts = {target: dict(name=names.get(target), participants=len(members),
                              before=len({comparison_map.get(i, i) for i in members}),
                              after=len({merges.get(i, (i,))[0] for i in members}))
                  for target, members in sorted(groups.items()) if len(members) > 1}
    example_comparison = {prefix: dict(before=previous['summary']['example_families'][prefix]['after'],
                                       after=row['after']) for prefix, row in family_counts.items()} if previous else None
    summary = dict(participants_scanned=len(ids), frequent_participants=sum(counts[i] >= communications.MIN_REFERENCE_NAME for i in ids),
                   participants_without_evidence=sum(not counts[i] for i in ids),
                   variant_nodes_merged=len(family), families=len({r['canonical'] for r in family}),
                   newly_merged_nodes=sum(r['previous_canonical'] == r['member'] for r in changed),
                   newly_merged_families=len({r['canonical'] for r in changed if r['previous_canonical'] == r['member']}),
                   changed_canonical_nodes=len(changed), changed_families=len({r['canonical'] for r in changed}),
                   surname_distance_over_two=len(suspicious),
                   distance_review_counts=dict(Counter(r['distance_review'] for r in suspicious)),
                   rule_counts=dict(sorted(Counter(r['rule'].split(':', 1)[1] for r in family).items())),
                   excluded_nonperson_canonicals=nonperson,
                   example_families=family_counts,
                   new_merges_all_rules=sum(i not in baseline and i in merges for i in ids),
                   previously_merged_now_separate=sum(i in baseline and i not in merges for i in ids),
                   net_participant_canonical_reduction=(len({baseline.get(i, i) for i in ids})
                                                       - len({merges.get(i, (i,))[0] for i in ids})),
                   suspicious_unexplained=len(unexplained),
                   peak_memory_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1))
    summary['all_families_scanned'] = len(all_counts)
    summary['families_with_fewer_nodes'] = sum(row['after'] < row['before'] for row in all_counts.values())
    summary['families_with_more_nodes'] = sum(row['after'] > row['before'] for row in all_counts.values())
    summary['context_only_spelling_merges_for_review'] = len(context_only_spelling)
    summary['changed_context_only_spelling_merges_for_review'] = len(new_context_only)
    rejection_counts = Counter(r['reason'] for r in rejections)
    summary['rejection_counts'] = dict(rejection_counts)
    path = root / 'data/private/audit' / args.output
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Open with restricted permissions even on first creation.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(dict(summary=summary, sample=sample, suspicious=unexplained,
                       surname_distance_over_two=suspicious, remaining_example_families=remaining,
                       merges=family, participant_identity_map=participant_merges,
                       rejected_merges=rejections, unresolved_candidates=unresolved,
                       family_node_counts=all_counts, example_family_comparison=example_comparison,
                       comparison_audit=args.compare,
                       context_only_spelling_merges_for_review=context_only_spelling), stream, indent=2, ensure_ascii=False)
        stream.write('\n')
    path.chmod(0o600)
    # Family keys are private labels too; stdout contains aggregate numbers only.
    print(json.dumps({k: v for k, v in summary.items() if k != 'example_families'}, sort_keys=True))


if __name__ == '__main__':
    main()
