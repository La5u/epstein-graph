"""Model review of likely owners of full-text email addresses / phone numbers.

    .venv/bin/python scripts/review_contact_owners.py ask --model gpt-6-luna [--limit N]
    .venv/bin/python scripts/review_contact_owners.py ask --model gpt-6.1-sol
    .venv/bin/python scripts/review_contact_owners.py apply

build_fulltext_contacts.py proposes an owner for a value when one name is
written next to it in >= 80% of its named documents (nodes.owner). This asks a
model per value, with the competing names and up to 3 text snippets, whether
the value is that person's own address / number. Answers are cached per model
in data/private/contact_owner_review/<model>.jsonl (they quote real values).

apply keeps owners both models confirm at medium or high confidence:
- a header address whose owner is a graph person or header name -> a merge in
  config/identity_merges.json (build_communications moves its records);
- every other value -> config/contact_owners.json ({value id: owner id}; IDs
  only), which build_fulltext_contacts uses as the value's canonical.
Run by hand (model calls); the build only reads the configs.
"""
import argparse
import json
import re
import sqlite3
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import corpus as corpus_source

ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / 'output/contact_graph.db'
RAW = ROOT / 'data/private/fulltext_contacts.db'
CACHE = ROOT / 'data/private/contact_owner_review'
OWNERS = ROOT / 'config/contact_owners.json'
MERGES = ROOT / 'config/identity_merges.json'
BATCH = 25

PROMPT = '''You review contact data extracted from the Epstein document release (court records, emails, bank papers).
Each line below is one email address or phone number, the person our pipeline thinks it belongs to, how often each
name was written right next to it, and up to 3 text snippets around it. Decide whether the value is that person's OWN
address or number (personal, mobile, home, or their direct office line / own work address).
- "owner": it is theirs.
- "not_owner": it belongs to someone else, or to an organisation (a firm's switchboard / main line / shared fax, a
  bank service number, a generic mailbox like info@), or the name is a sender/recipient that is not the value's holder.
- "unsure": the snippets do not show it.
OCR damage in names and values is common (jeevacation -> leevacation); treat obvious OCR variants of the same name as
the same person.
Answer with one JSON object per line and nothing else:
{"id": <line id>, "verdict": "owner" | "not_owner" | "unsure", "confidence": "high" | "medium" | "low", "why": "<= 15 words"}

'''


def private_write(path, text):
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)


def lookups():
    names = {}
    for name in ('contact_lookup.json', 'fulltext_contact_lookup.json'):
        path = ROOT / 'data/private' / name
        if path.exists():
            names.update({k: v['value'] for k, v in json.loads(path.read_text()).items()})
    for e in json.loads((ROOT / 'output/normalized_entities.json').read_text()):
        names[f"kg:{e['id']}"] = names[str(e['id'])] = e['name']
    return names


def value_pattern(kind, value):
    """Loose regex for a value as written in the text (phones: any separators)."""
    if kind == 'email':
        return re.compile(re.escape(value), re.I)
    digits = value[2:] if value.startswith('+1') else value[1:]
    return re.compile(r'[\s().-]{0,3}'.join(map(re.escape, digits[-10:])))


def items(limit=None):
    """[(value id, text for the model)] for every value with a proposed owner."""
    names = lookups()
    corpus = corpus_source.connect()
    out = []
    with closing(sqlite3.connect(f'file:{GRAPH}?mode=ro', uri=True)) as graph, \
            closing(sqlite3.connect(f'file:{RAW}?mode=ro', uri=True)) as raw:
        rows = graph.execute('SELECT id, kind, owner, in_headers, documents FROM nodes WHERE owner IS NOT NULL ORDER BY documents DESC').fetchall()
        for identity, kind, owner, in_headers, documents in rows[:limit]:
            value = names.get(identity)
            if not value:
                continue
            counts = graph.execute('SELECT identity, documents FROM names WHERE value = ? ORDER BY documents DESC LIMIT 5',
                                   (identity,)).fetchall()
            written = '; '.join(f'{names.get(i, i)} x{n}' for i, n in counts)
            snippets = []
            pattern = value_pattern(kind, value)
            for efta, page in raw.execute('SELECT efta, page FROM mentions WHERE kind = ? AND value = ? AND name IS NOT NULL LIMIT 12',
                                          (kind, value)):
                row = corpus.execute('SELECT text FROM pages WHERE doc_id = ? AND page = ?', (efta, page)).fetchone()
                found = pattern.search(row[0] or '') if row else None
                if found:
                    text = row[0][max(0, found.start() - 160):found.end() + 60]
                    snippets.append(re.sub(r'\s+', ' ', text).strip())
                if len(snippets) >= 3:
                    break
            out.append((identity, f'{kind} {value} | proposed owner: {names.get(owner, owner)} | in email headers: '
                                  f'{"yes" if in_headers else "no"} | documents: {documents} | names written next to it: {written} | '
                                  'snippets: ' + ' || '.join(f'"{s}"' for s in snippets)))
    corpus.close()
    return out


def ask(prompt, model):
    result = subprocess.run(['sol', '-m', model, '-e', 'low'], input=prompt, capture_output=True, text=True, timeout=1800)
    if result.returncode == 75:
        raise SystemExit('sol: usage limit reached')
    answers = []
    for line in result.stdout.splitlines():
        line = line.strip().strip(',')
        if line.startswith('{'):
            try:
                answers.append(json.loads(line))
            except ValueError:
                pass
    return answers


def cached(model):
    path = CACHE / f'{model}.jsonl'
    found = {}
    if path.exists():
        for line in path.read_text().splitlines():
            item = json.loads(line)
            found[item['value']] = item
    return found


def run(args):
    done = cached(args.model)
    todo = [(i, text) for i, text in items(args.limit) if i not in done]
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    print(f'{len(todo)} values to review in {len(batches)} batches ({len(done)} cached)', flush=True)
    path = CACHE / f'{args.model}.jsonl'

    def one(batch):
        answers = ask(PROMPT + '\n'.join(f'{n}\t{text}' for n, (_, text) in enumerate(batch)), args.model)
        return [{**a, 'value': batch[int(a['id'])][0]} for a in answers
                if str(a.get('id', '')).isdigit() and int(a['id']) < len(batch)]

    lines = [json.dumps(item) for item in done.values()]
    with ThreadPoolExecutor(args.workers) as pool:
        for n, answers in enumerate(pool.map(one, batches), 1):
            lines += [json.dumps(a, ensure_ascii=False) for a in answers]
            private_write(path, '\n'.join(lines) + '\n')
            print(f'batch {n}/{len(batches)}: {len(answers)} answers', flush=True)


def apply(args):
    a, b = cached('gpt-6-luna'), cached('gpt-6.1-sol')
    good = lambda x: x and x.get('verdict') == 'owner' and x.get('confidence') in ('high', 'medium')
    agreed = {v for v in a if good(a[v]) and good(b.get(v))}
    with closing(sqlite3.connect(f'file:{GRAPH}?mode=ro', uri=True)) as graph:
        info = {i: (owner, in_headers) for i, owner, in_headers in graph.execute(
            'SELECT id, owner, in_headers FROM nodes WHERE owner IS NOT NULL')}
    merges = json.loads(MERGES.read_text())
    taken = {str(m) for item in merges['merges'] for m in item['members']}
    blocked = {frozenset(map(str, pair)) for pair in merges.get('never_merge', []) if isinstance(pair, list)}
    owners = json.loads(OWNERS.read_text()) if OWNERS.exists() else {'_doc': '', 'owners': {}}
    owners['_doc'] = ('Reviewed owners of full-text contact values (review_contact_owners.py: Luna and Sol both '
                      'said owner, medium/high). IDs only; used by build_fulltext_contacts as the canonical.')
    added = Counter()
    for value in sorted(agreed):
        if value not in info:
            continue
        owner, in_headers = info[value]
        canonical = int(owner) if owner.isdigit() else owner
        # A header address can only merge into an identity build_communications knows (graph person / header name).
        if in_headers and (owner.isdigit() or owner in lookup_participants()):
            if value in taken or frozenset((value, str(canonical))) in blocked:
                added['skipped_already_merged'] += 1
                continue
            merges['merges'].append({'canonical': canonical, 'members': [value], 'confidence': 'probable',
                                     'reason': 'name written next to the address in the full text; contact owner review '
                                               '(Luna + Sol agree); evidence in data/private/contact_owner_review/'})
            taken.add(value)
            added['identity_merges'] += 1
        else:
            owners['owners'][value] = owner
            added['contact_owners'] += 1
    MERGES.write_text(json.dumps(merges, indent=1, ensure_ascii=False) + '\n')
    OWNERS.write_text(json.dumps(owners, indent=1) + '\n')
    print(json.dumps({'agreed_owner': len(agreed), 'luna_answers': len(a), 'sol_answers': len(b), **added}))


_participants = None


def lookup_participants():
    """Header-name identities (exist in build_communications, unlike new full-text names)."""
    global _participants
    if _participants is None:
        lookup = json.loads((ROOT / 'data/private/contact_lookup.json').read_text())
        _participants = {k for k, v in lookup.items() if v.get('kind') == 'participant'}
    return _participants


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('command', choices=('ask', 'apply'))
    parser.add_argument('--model', default='gpt-6-luna')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--workers', type=int, default=6)
    args = parser.parse_args()
    run(args) if args.command == 'ask' else apply(args)


if __name__ == '__main__':
    main()
