"""PRIVATE, local-only: model review of every visible viewer label that has no review verdict yet.

    .venv/bin/python -B scripts/label_review.py dump              # decrypt output/graph.html -> label list
    .venv/bin/python -B scripts/label_review.py ask [--limit N]   # Terra reads labels in batches (resumable)
    .venv/bin/python -B scripts/label_review.py recheck           # second read (high effort) of low-confidence text/junk/unsure
    .venv/bin/python -B scripts/label_review.py report            # verdict counts and samples
    .venv/bin/python -B scripts/label_review.py apply             # write config/label_review.json (IDs + label hashes)

Real labels go to the model (`terra`); labels and answers stay in data/private/label_review/.
Survivors, contacts and websites are never sent. Answers are keyed by viewer id; a label
that changed since it was answered is asked again.
"""
import argparse
import json
import os
import random
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / 'data/private/label_review'
LABELS = DIR / 'viewer_labels.json'
TYPES = ('person', 'participant', 'organization', 'place', 'vehicle')
BATCH, WORKERS = 100, 6
VERDICTS = ('name', 'misread_name', 'wrong_type', 'role_or_group', 'text', 'junk', 'unsure')

PROMPT = '''You review node labels of a graph built from the Epstein files (OCR of DOJ-released emails, flight logs,
court and bank documents). Many labels are parser mistakes: a sentence fragment, a form field, a date, markup or
OCR noise taken for a name. Others are real names with OCR damage, which must be kept.

Each input line: id <TAB> label <TAB> current type <TAB> records <TAB> other spellings merged into it (may be empty).
Types: person, participant (a name from an email header that matched no known person; may be a person, company or
role), organization, place, vehicle.

Answer one verdict per line:
- "name": a real name of a person, organisation, place or vehicle, and the current type fits (participant fits any)
- "misread_name": a real name with OCR damage; give the corrected spelling in "fix" only if you are sure
- "wrong_type": a real name but the type is wrong; give "type" (person, organization, place, vehicle)
- "role_or_group": an office, desk, team, family, mailing list or job title ("Certified Paralegal", "Flight Status")
- "text": not a name: words, a phrase or sentence, a date, a number, a form label, a bare legal form ("Laboratories Inc.")
- "junk": OCR noise, markup, a fragment of a word, a broken email address
- "unsure"
A single first name, surname, nickname or initials is a "name" (or "misread_name"), never "text". A rare foreign or
unusual name is a name. Brand and product names are organisation names. Only say "text" or "junk" when no reading of
the label is a name.

Output ONLY JSON lines in input order, nothing else:
{"id": "...", "v": "<verdict>", "c": "high|low", "fix": "...", "type": "..."}   (fix/type only when used)

'''


def private_write(path, text):
    DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def dump(_args):
    sys.path.insert(0, str(ROOT / 'scripts'))
    import audit_nodes
    nodes, decrypted, warnings = audit_nodes.read_viewer(ROOT / 'output/graph.html', ROOT / 'data/private/viewer_password')
    if not decrypted:
        sys.exit(f'viewer not decrypted: {warnings}')
    out = []
    for n in nodes:
        rec = n.get('rec') or {}
        label = n.get('real') or n['label']
        others = []
        for m in rec.get('merged_identities') or []:
            v = m.get('value')
            if isinstance(v, str) and v != label and v not in others and '@' not in v:
                others.append(v)
        out.append({'id': str(n['id']), 'label': label, 'type': n['type'], 'deg': n.get('deg'), 'others': others[:3]})
    private_write(LABELS, json.dumps(out, ensure_ascii=False))
    print(len(out), 'labels ->', LABELS.relative_to(ROOT))


def reviewed_ids():
    review = json.loads((ROOT / 'config/node_review.json').read_text())
    return {k for section in ('participants', 'persons') for k in review.get(section, {})}


def candidates():
    done = reviewed_ids()
    return [n for n in json.loads(LABELS.read_text()) if n['type'] in TYPES and n['id'] not in done]


def answers_path(model, suffix=''):
    return DIR / f'answers_{model}{suffix}.json'


def ask_model(prompt, model, effort):
    result = subprocess.run(['terra', '-m', model, '-e', effort, '-C', str(DIR)], input=prompt,
                            capture_output=True, text=True, timeout=1800)
    if result.returncode == 75:
        raise SystemExit('usage limit: ' + result.stderr.strip())
    answers = []
    for line in result.stdout.splitlines():
        line = line.strip().strip('`')
        if line.startswith('{'):
            try:
                answers.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return answers


def ask(args):
    path = answers_path(args.model, args.suffix)
    done = json.loads(path.read_text()) if path.exists() else {}
    items = [n for n in candidates() if done.get(n['id'], {}).get('label') != n['label']]
    if args.only is not None:
        items = [n for n in items if n['id'] in args.only]
    if args.sample:
        random.seed(11)
        items = random.sample(items, min(args.sample, len(items)))
    items = items[:args.limit or None]
    print(len(items), 'labels to ask', flush=True)
    batches = [items[i:i + BATCH] for i in range(0, len(items), BATCH)]

    def one(batch):
        lines = '\n'.join('\t'.join([n['id'], n['label'], n['type'], str(n['deg'] or 0), ' | '.join(n['others'])])
                          for n in batch)
        return batch, ask_model(PROMPT + lines, args.model, args.effort)

    with ThreadPoolExecutor(WORKERS) as pool:
        for k, (batch, answers) in enumerate(pool.map(one, batches), 1):
            by_id = {str(a.get('id')): a for a in answers if a.get('v') in VERDICTS}
            for n in batch:
                if n['id'] in by_id:
                    done[n['id']] = {**by_id[n['id']], 'label': n['label'], 'node_type': n['type']}
            private_write(path, json.dumps(done, ensure_ascii=False, indent=0))
            print(f'batch {k}/{len(batches)}: {len(by_id)}/{len(batch)} answered', flush=True)


def recheck(args):
    first = json.loads(answers_path(args.model).read_text())
    args.only = {i for i, a in first.items() if a['v'] in ('text', 'junk', 'unsure') and a['c'] != 'high'}
    args.suffix, args.effort = '_recheck', 'high'
    ask(args)


def final_answers(model):
    """First read, with the high-effort second read replacing it where one exists."""
    done = json.loads(answers_path(model).read_text())
    second = answers_path(model, '_recheck')
    if second.exists():
        done.update({i: {**a, 'recheck': True} for i, a in json.loads(second.read_text()).items()})
    return done


def apply(args):
    sys.path.insert(0, str(ROOT / 'scripts'))
    from viewer_label_payload import LABEL_REVIEW, RETYPES, label_signature
    current = {n['id']: n for n in json.loads(LABELS.read_text())}
    answers = final_answers(args.model)
    # A "text" label that contains another node's confirmed full name ("Does the Jeffrey Epstein VI
    # Foundation") is a name with noise around it: it needs stripping, not hiding, so it stays shown.
    review = json.loads((ROOT / 'config/node_review.json').read_text())
    ok = {k for section in ('participants', 'persons') for k, v in review.get(section, {}).items()
          if v.get('verdict') in ('ok_person', 'ok_entity')}
    kinds = {'person', 'participant', 'organization'}
    sized = lambda label: len(label.split()) >= 2 and len(label) >= 10
    names = {a['label'].casefold() for a in answers.values()
             if a['v'] == 'name' and a['c'] == 'high' and a['node_type'] in kinds and sized(a['label'])}
    names |= {n['label'].casefold() for i, n in current.items() if i in ok and sized(n['label'])}
    out, counts = {}, Counter()
    for i, a in sorted(answers.items()):
        n = current.get(i)
        if not n or n['label'] != a['label'] or a['c'] != 'high':
            continue
        if a['v'] in ('text', 'junk'):
            label = n['label'].casefold()
            if any(name in label for name in names if name != label):
                counts['kept: contains a name'] += 1
                continue
            out[i] = {'v': 'not_a_name', 'sig': label_signature(n['label'])}
        elif a['v'] == 'wrong_type' and a.get('type') in RETYPES and a['type'] != n['type'] and n['type'] != 'participant':
            out[i] = {'v': 'retype', 'type': a['type'], 'sig': label_signature(n['label'])}
        else:
            continue
        counts[f"{n['type']} {out[i]['v']} {out[i].get('type') or ''}".strip()] += 1
    doc = ('Model label review (scripts/label_review.py, Terra, high confidence only). Viewer id -> not_a_name '
           '(hidden, never deleted) or retype; sig = sha256(real label)[:10], so a verdict lapses when the label '
           'changes. Explicit merges retain their source/target label hashes and canonical publication decisions. No names here.')
    previous = json.loads(LABEL_REVIEW.read_text()) if LABEL_REVIEW.exists() else {}
    config = {'_doc': doc, 'by': f'{args.model} 2026-11-11', 'labels': out}
    if previous.get('merges'):
        config['merges'] = previous['merges']  # applying model verdicts must not erase reviewed identity decisions
    LABEL_REVIEW.write_text(json.dumps(config, indent=1) + '\n')
    print(len(out), 'verdicts ->', LABEL_REVIEW.relative_to(ROOT))
    for k, v in counts.most_common():
        print(' ', k, v)


def report(args):
    done = final_answers(args.model)
    print(len(done), 'answers')
    print(Counter((a['node_type'], a['v'], a['c']) for a in done.values()).most_common(40))
    random.seed(3)
    for v in VERDICTS:
        pick = [a for a in done.values() if a['v'] == v]
        print(f'\n== {v} ({len(pick)})')
        for a in random.sample(pick, min(args.show, len(pick))):
            extra = a.get('fix') or a.get('type') or ''
            print(f"  {a['node_type']:12} {a['c']:4} {a['label']!r} {extra}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('cmd', choices=['dump', 'ask', 'recheck', 'report', 'apply'])
    p.add_argument('--model', default='gpt-6.1-terra')
    p.add_argument('--effort', default='medium')
    p.add_argument('--limit', type=int)
    p.add_argument('--sample', type=int, help='ask: a seeded random sample of this many labels')
    p.add_argument('--show', type=int, default=15)
    args = p.parse_args()
    args.only, args.suffix = None, ''
    {'dump': dump, 'ask': ask, 'recheck': recheck, 'report': report, 'apply': apply}[args.cmd](args)


if __name__ == '__main__':
    main()
