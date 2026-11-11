"""Compare bounded header replay reports against the private hand-read audit.

Run after report_header_merges.py --resolve --review. This reads the small viewer
snapshot, reports and findings, never the combined graphs or the raw corpus.
Judgments are deliberately separate from these mechanical recall counts.
"""
import argparse
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from build_communications import ROOT


def graph_id(i):
    return 'kg:' + str(i) if str(i).isdigit() else str(i)


def root(i, mapping):
    seen = set()
    while i in mapping and i not in seen:
        seen.add(i)
        i = mapping[i]['target']
    return i


def compare(snapshot, findings, unresolved, before, after):
    labels = {graph_id(n['id']): n.get('real') or n['label'] for n in snapshot}
    by_label = defaultdict(set)
    for i, label in labels.items():
        by_label[label].add(i)
    by_labels = defaultdict(set)
    flagged = set()
    for f in findings:
        ids = {graph_id(i) for i in f.get('good', f['ids'])} & labels.keys()
        by_labels[tuple(f['labels'])].update(ids)
        flagged.update(ids)
    old, new = before['resolved_map'], after['resolved_map']
    changed = {i for i in labels if root(i, old) != root(i, new)}
    by_rule = defaultdict(set)
    for i in changed:
        rule = new.get(i, {}).get('rule', '')
        if not rule.startswith('header_context:'):
            rule = new.get(root(i, old), {}).get('rule', rule)
        by_rule[rule].add(i)
    for i, repair in after.get('label_repairs', {}).items():
        by_rule['strip:' + repair['rule']].add(i)
    by_rule['compound'].update(c['id'] for c in after.get('compounds', []))
    all_touched = set().union(*by_rule.values())
    groups = []
    for group in unresolved:
        ids = by_labels.get(tuple(group)) or set().union(*(by_label.get(label, set()) for label in group))
        earlier, later = {root(i, old) for i in ids}, {root(i, new) for i in ids}
        if len(ids) == 1:
            status = 'fixed' if ids <= changed else 'untouched'
        elif len(later) < len(earlier):
            status = 'fixed' if len(later) == 1 else 'partial'
        else:
            status = 'untouched'
        groups.append(dict(labels=group, ids=sorted(ids), before_roots=sorted(earlier), after_roots=sorted(later),
                           status=status, stripped_or_compound=bool(ids & (all_touched - changed))))
    rules = {}
    rng = random.Random(3)
    for rule, ids in sorted(by_rule.items()):
        unflagged = sorted(ids - flagged)
        sample = rng.sample(unflagged, min(30, len(unflagged)))
        rules[rule] = dict(touched=len(ids), flagged=len(ids & flagged), unflagged=len(unflagged),
                           unresolved_groups=dict(Counter(g['status'] for g in groups if set(g['ids']) & ids)),
                           flagged_ids=sorted(ids & flagged), unflagged_ids=unflagged,
                           unflagged_sample=[dict(id=i, label=labels[i], target_label=labels.get(root(i, new), root(i, new))) for i in sample])
    # Exact reviewer classes remain available instead of treating loosely named
    # or low-confidence findings as ground truth. Denominators are viewer person,
    # participant and contact nodes, the scope of this task.
    scoped = {graph_id(n['id']) for n in snapshot if n['type'] in ('person', 'participant', 'contact')}
    classes = defaultdict(set)
    for f in findings:
        classes[f['class']].update({graph_id(i) for i in f.get('good', f['ids'])} & scoped)
    class_counts = {kind: dict(flagged=len(ids), fixed_or_stripped_or_marked=len(ids & all_touched),
                               untouched=len(ids - all_touched)) for kind, ids in sorted(classes.items()) if ids}
    themes = {}
    for theme, pattern in [('partial_names', r'first.*name|surname|trunc|initial|handle|abbrev'),
                           ('ocr_names', r'ocr|misspell|fontanilla'),
                           ('decorated_names', r'prefix|suffix|title|leading|trailing|noise'),
                           ('compound_names', r'compound|combined.*name|two.person|two.people|couple')]:
        relevant = {kind for kind in classes if re.search(pattern, kind, re.I)
                    and not re.search(r'org|company|legal|junk|garbage', kind, re.I)}
        ids = set().union(*(classes[kind] for kind in relevant)) if relevant else set()
        themes[theme] = dict(flagged=len(ids), touched=len(ids & all_touched), untouched=len(ids - all_touched),
                             reviewer_classes=sorted(relevant))
    return dict(summary=dict(unresolved_groups=len(groups), recall=dict(Counter(g['status'] for g in groups)),
                             unresolved_with_presentation_or_compound_change=sum(g['stripped_or_compound'] for g in groups),
                             viewer_nodes_newly_merged=len(changed), rules={r: {k: v for k, v in values.items() if isinstance(v, int)} for r, values in rules.items()}),
                rules=rules, reviewer_classes=class_counts, reviewer_themes=themes, groups=groups)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path, default=ROOT / 'data/private/audit/header_merge_before_context.json')
    parser.add_argument('--after', type=Path, default=ROOT / 'data/private/audit/header_merge_report.json')
    parser.add_argument('--out', type=Path, default=ROOT / 'data/private/audit/header_followup3_evaluation.json')
    args = parser.parse_args()
    if not args.out.resolve().is_relative_to((ROOT / 'data/private').resolve()):
        raise SystemExit('Private evaluation must stay under data/private/')
    def read(path):
        return json.loads(path.read_text())
    result = compare(read(ROOT / 'data/private/audit/viewer_nodes.json'),
                     read(ROOT / 'data/private/audit/full_read/all_findings.json'),
                     read(ROOT / 'data/private/audit/a_unfixed_findings.json'), read(args.before), read(args.after))
    from private_contacts import write_private
    write_private(args.out, json.dumps(result, indent=1, ensure_ascii=False) + '\n')
    print(json.dumps(result['summary'], indent=1))


if __name__ == '__main__':
    main()
