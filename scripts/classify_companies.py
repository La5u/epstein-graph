"""Stage 2 of the company layer: model review of output/company_candidates.csv.

Each candidate (status candidate/kg, >= --min-docs documents incl. folded
spellings) is shown with its spellings and up to three short snippets from
different documents. The model answers kind, relevance and a clean display
name. Answers are cached per model in output/.build/company_review_<model>.jsonl
(keyed on the candidate key), so reruns only ask for new candidates.

    .venv/bin/python scripts/classify_companies.py [--model gpt-6-luna|gpt-6.1-terra|gpt-6.1-sol|deepseek] [--limit N] [--workers 6]
        [--missing-from gpt-6.1-sol]
"""
import argparse
import csv
import json
import random
import re
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import corpus as corpus_source

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'
CANDIDATES = OUT / 'company_candidates.csv'
MENTIONS = OUT / '.build/company_mentions.db'
BATCH = 50
KINDS = ('company', 'trust', 'foundation', 'fund_or_product', 'bank_or_broker', 'law_firm', 'government', 'media',
         'other_org', 'generic', 'junk')
RELEVANCE = ('epstein_controlled', 'network', 'incidental')
PROMPT = f'''Each item is a name ending in a legal form (LLC, Inc., Ltd., Trust, Foundation, Fund, ...) found by a regex in the
Epstein files (DOJ releases: bank statements, emails, court filings, contracts, flight logs, news). Spellings are OCR'd,
so letters may be damaged. For each item answer one JSON object per line (no prose, no code fences) with keys:
  "key": the item key exactly as given,
  "kind": one of {', '.join(KINDS)}
     (fund_or_product = an investment fund, share class, money-market or mutual fund product;
      generic = a common phrase, not one named entity ("The Company", "Underlying Fund", "Limited Liability Company");
      junk = OCR garbage, a fragment of a longer name, or words glued onto a name),
  "relevance": one of {', '.join(RELEVANCE)}
     (epstein_controlled = owned, controlled, funded or set up by Jeffrey Epstein or his estate/staff: his LLCs, trusts,
      foundations, shells, aircraft and property companies;
      network = a company/trust/foundation of a person around Epstein, a counterparty or adviser in his dealings, a firm
      that acted for or against him in the cases;
      incidental = a vendor, product, bank-statement boilerplate, news subject, or anything else),
  "name": the entity's name as it should be displayed, correctly capitalized and spelled (e.g. "JEGE, LLC",
      "Southern Trust Company, Inc."), or "" for generic/junk,
  "confidence": "high" | "medium" | "low".
Use your knowledge of the Epstein story where it helps. Output nothing but the JSON lines.
'''


def candidates(min_docs):
    rows = [r for r in csv.DictReader(open(CANDIDATES)) if r['status'] in ('candidate', 'kg')
            and int(r['documents_with_folded'] or r['documents']) >= min_docs]
    rows.sort(key=lambda r: -int(r['documents_with_folded'] or r['documents']))
    return rows


def snippets(keys, per_key=3, width=160):
    """{key: [snippet]} from different documents (random, reproducible)."""
    out = {}
    corpus = corpus_source.connect()
    with closing(sqlite3.connect(f'file:{MENTIONS}?mode=ro', uri=True)) as db:
        for key in keys:
            rows = db.execute('SELECT spelling, page_id, efta FROM mentions WHERE key = ?', (key,)).fetchall()
            random.Random(key).shuffle(rows)
            seen, found = set(), []
            for spelling, page_id, efta in rows:
                if efta in seen:
                    continue
                seen.add(efta)
                page = corpus.execute('SELECT text FROM pages WHERE page_id = ?', (page_id,)).fetchone()
                text = (page[0] if page else '') or ''  # supplement pages are not in the corpus
                at = text.find(spelling.split()[0])
                at = text.find(spelling) if spelling in text else at
                if at < 0:
                    continue
                piece = re.sub(r'\s+', ' ', text[max(0, at - width):at + len(spelling) + width])
                found.append(f'[{efta}] ...{piece}...')
                if len(found) >= per_key:
                    break
            out[key] = found
    return out


DEEPSEEK = 'opencode-go/deepseek-v4.1-flash'  # unlimited plan (2026-10-30)


def ask(prompt, model):
    if model == 'deepseek':  # hosted model; public corpus text only, short timeout (a call can hang)
        try:
            result = subprocess.run(['opencode', 'run', '--pure', '-m', DEEPSEEK], input=prompt, capture_output=True,
                                    text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return []
    else:
        result = subprocess.run(['sol', '-m', model, '-e', 'low'], input=prompt, capture_output=True, text=True, timeout=1800)
        if result.returncode == 75:
            raise SystemExit('sol: usage limit reached')
    answers = []
    for line in result.stdout.splitlines():
        line = re.sub(r'\x1b\[[0-9;]*m', '', line).strip().strip(',')  # opencode colours its output
        if line.startswith('{'):
            try:
                answers.append(json.loads(line))
            except ValueError:
                pass
    return answers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='gpt-6-luna')
    parser.add_argument('--min-docs', type=int, default=10)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--sample', type=int, default=0, help='a random sample of N candidates instead of the top')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--missing-from', help='skip candidates this model already answered')
    parser.add_argument('--keys', help='JSON list of candidate keys to classify (default: all)')
    args = parser.parse_args()
    cache = OUT / f'.build/company_review_{args.model}.jsonl'
    done = {}
    if cache.exists():
        for line in cache.read_text().splitlines():
            item = json.loads(line)
            done[item['key']] = item
    rows = candidates(args.min_docs)
    if args.sample:
        rows = random.Random(7).sample(rows, min(args.sample, len(rows)))
    if args.missing_from:  # only candidates another model has not answered (e.g. after Sol's usage limit)
        other = OUT / f'.build/company_review_{args.missing_from}.jsonl'
        answered = {json.loads(line)['key'] for line in other.read_text().splitlines()} if other.exists() else set()
        rows = [r for r in rows if r['key'] not in answered]
    if args.keys:
        wanted = set(json.loads(Path(args.keys).read_text()))
        rows = [r for r in rows if r['key'] in wanted]
    rows = [r for r in rows if r['key'] not in done][:args.limit or None]
    print(f'{len(rows)} to classify with {args.model} ({len(done)} cached)')
    size = 15 if args.model == 'deepseek' else BATCH  # the free model is slow on long prompts
    batches = [rows[i:i + size] for i in range(0, len(rows), size)]

    def run(batch):
        found = snippets([r['key'] for r in batch])
        body = '\n\n'.join(f"key: {r['key']}\ndocuments: {r['documents_with_folded'] or r['documents']}\n"
                           f"spellings: {r['spellings']}\n" + ''.join(f'  {s}\n' for s in found[r['key']]) for r in batch)
        answers = ask(PROMPT + '\nITEMS:\n\n' + body, args.model)
        wanted = {r['key'] for r in batch}
        return [a for a in answers if a.get('key') in wanted and a.get('kind') in KINDS]

    with ThreadPoolExecutor(args.workers) as pool, open(cache, 'a') as f:
        for answers in pool.map(run, batches):
            for a in answers:
                f.write(json.dumps(a, ensure_ascii=False) + '\n')
            f.flush()
            print(f'+{len(answers)}', flush=True)


if __name__ == '__main__':
    main()
