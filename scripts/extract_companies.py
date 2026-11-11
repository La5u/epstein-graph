"""Company / trust / shell candidates from the full-text corpus (stage 1: candidates only).

A candidate is a run of capitalized words ending in a legal-form suffix
("JEGE, LLC", "Southern Trust Company", "HBRK Associates Inc."), found by a
regex over every page. Spellings are grouped by a key that ignores case,
punctuation, a leading "The" and the suffix's form (Inc. = Incorporated,
Ltd = Limited). Nothing here enters the graph: the output is a ranked list
for review (output/company_candidates.csv) and the mentions behind it
(output/.build/company_mentions.db: key, page id).

    .venv/bin/python scripts/extract_companies.py [--workers 8] [--limit-pages N]
"""
import argparse
import csv
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from pathlib import Path

import numpy as np
from rapidfuzz.distance import Levenshtein

import corpus as corpus_source
from corpus_index import page_map
from supplement import SUPPLEMENT_BASE, supplement_pages

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'
MENTIONS = OUT / '.build/company_mentions.db'
CANDIDATES = OUT / 'company_candidates.csv'

# Legal-form suffix -> canonical form used in the key.
SUFFIXES = {
    'llc': 'llc', 'l.l.c.': 'llc', 'l.l.c': 'llc', 'inc': 'inc', 'inc.': 'inc', 'incorporated': 'inc',
    'ltd': 'ltd', 'ltd.': 'ltd', 'limited': 'ltd', 'corp': 'corp', 'corp.': 'corp', 'corporation': 'corp',
    'lp': 'lp', 'l.p.': 'lp', 'l.p': 'lp', 'llp': 'llp', 'l.l.p.': 'llp', 'trust': 'trust', 'foundation': 'foundation',
    'holdings': 'holdings', 'fund': 'fund', 'company': 'co', 'co.': 'co', 'gmbh': 'gmbh', 'ag': 'ag', 's.a.': 'sa',
    'n.v.': 'nv', 'b.v.': 'bv', 'plc': 'plc', 'pllc': 'pllc', 'p.a.': 'pa', 'p.c.': 'pc', 'lllp': 'lllp',
    # Foreign forms (Liechtenstein, Swiss, Austrian, French, Italian, Australian, Nordic vehicles).
    'anstalt': 'anstalt', 'stiftung': 'stiftung', 'establishment': 'establishment', 'sarl': 'sarl', 's.a.r.l.': 'sarl',
    's.p.a.': 'spa', 'spa': 'spa', 'srl': 'srl', 's.r.l.': 'srl', 'pty': 'pty', 'pty.': 'pty', 'kg': 'kg', 'ab': 'ab', 'oy': 'oy',
    'a/s': 'as', 'aps': 'aps', 'sas': 'sas', 's.a.s.': 'sas', 'bvba': 'bvba', 'kft': 'kft', 'sp. z o.o.': 'spzoo',
}
WORD_FORMS = {'company', 'corporation', 'incorporated', 'limited'}  # spelled-out forms inside a name
SUFFIX = r"(?:L\.?L\.?C\.?|PLLC|LLLP|LLP|L\.L\.P\.|Inc\.?|INC\.?|Incorporated|INCORPORATED|Ltd\.?|LTD\.?|Limited|LIMITED|" \
         r"Corp\.?|CORP\.?|Corporation|CORPORATION|L\.?P\.?|Trust|TRUST|Foundation|FOUNDATION|Holdings|HOLDINGS|" \
         r"Fund|FUND|Company|COMPANY|Co\.|CO\.|GmbH|GMBH|AG|S\.A\.|N\.V\.|B\.V\.|PLC|plc|P\.A\.|P\.C\.|" \
         r"Anstalt|ANSTALT|Stiftung|STIFTUNG|Establishment|ESTABLISHMENT|S\.?[aA]\.?[rR]\.?[lL]\.?|SARL|S\.p\.A\.|SpA|S\.r\.l\.|SRL|" \
         r"Pty\.?(?:[ \t]+Ltd\.?)?|PTY|KG|AB|Oy|A/S|ApS|S\.A\.S\.|SAS|BVBA|Kft\.?)"
# A capitalized word (Title, ALLCAPS, or with digits/&/'/-, "J.P."), or a lower-case connector inside a name.
CAP = r"(?:[A-Z][A-Za-z0-9&'\-]*\.?|[A-Z]\.(?:[A-Z]\.)*|\d+[A-Za-z]*)"
CONNECT = r"(?:of|and|&|the|de|du|la|le|van|von|del|di|for|on)"
NAME = re.compile(rf"(?<![A-Za-z0-9&'\-.])({CAP}(?:[ \t]+(?:{CAP}|{CONNECT})){{0,6}}?),?[ \t]+({SUFFIX}(?:,?[ \t]+{SUFFIX}){{0,2}})(?![A-Za-z0-9])")
QUICK = re.compile(r"(?i)\b(?:llc|inc|incorporated|ltd|limited|corp|corporation|l\.?p|llp|trust|foundation|holdings|fund|company|co\.|gmbh|ag|s\.a\.|n\.v\.|b\.v\.|plc|p\.a\.|p\.c\.|anstalt|stiftung|establishment|s\.?a\.?r\.?l|s\.p\.a|spa|srl|s\.r\.l|pty|kg|ab|oy|a/s|aps|sas|bvba|kft)")
LEADING = {'the', 'a', 'an', 'and', 'of', 'for', 'to', 'by', 'from', 'with', 'in', 'on', 'at', 'as', 'or', 'your', 'our',
           'my', 'its', 'their', 'his', 'her', 'this', 'that', 'these', 'such', 'said', 'each', 'any', 'all', 'no', 're',
           'fw', 'fwd', 'cc', 'to', 'subject', 'from', 'sent', 'dear', 'mr', 'mrs', 'ms', 'dr'}
SENTENCE_BREAK = re.compile(r"(?<=[a-z]{3})[.:;!?][ \t]+|(?<=[A-Z]{4})[.:;!?][ \t]+")


def name_key(prefix, suffix):
    """Grouping key: lower-case words without punctuation, no leading article, canonical suffix."""
    # A suffix chain ("Trust Company, Inc.") keeps its inner words; the last one is the form.
    *inner, suffix = re.split(r',?[ \t]+', suffix.strip())
    prefix = ' '.join([prefix, *inner])
    # A digit before the company form is a common OCR reading of '&'.
    prefix = re.sub(r'\b8(?=\s+(?:Co\b|Company\b))', '&', prefix, flags=re.I)
    if suffix.casefold().rstrip('.') in ('co', 'company'):
        prefix = re.sub(r'\s+8$', ' &', prefix)
    words = [SUFFIXES.get(w, SUFFIXES.get(w + '.', w)) if w in WORD_FORMS else w
             for w in re.findall(r"[a-z0-9&]+", prefix.casefold().replace("'", ''))]
    while words and words[0] in LEADING:
        words.pop(0)
    while words and words[-1] in ('and', 'of', '&', 'the'):
        words.pop()
    if not words:
        return None
    form = suffix.casefold()
    canonical = SUFFIXES.get(form) or SUFFIXES.get(form.rstrip('.')) or SUFFIXES.get(form + '.') or form.strip('.')
    return ' '.join(words) + ' ' + canonical


def extract(text):
    """[(key, spelling)] on one page."""
    out = []
    if not text or not QUICK.search(text):
        return out
    for match in NAME.finditer(text):
        prefix, suffix = match.group(1), match.group(2)
        prefix = SENTENCE_BREAK.split(prefix)[-1]  # "Statement. American Express ..." -> the last sentence
        if not re.match(r"[A-Z0-9]", prefix):
            continue
        key = name_key(prefix, suffix)
        if key:
            out.append((key, f'{prefix} {suffix}'.strip()))
    return out


FORM_WORDS = {'llc', 'inc', 'ltd', 'corp', 'co', 'lp', 'llp', 'pllc', 'lllp', 'pa', 'pc', 'sa', 'ag', 'gmbh', 'nv', 'bv', 'plc'}
# Words that only make a name together with a distinctive one ("Underlying Fund", "Client Trust").
GENERIC = {'company', 'co', 'inc', 'llc', 'ltd', 'corp', 'lp', 'llp', 'pllc', 'pa', 'pc', 'trust', 'fund', 'funds', 'foundation',
           'holdings', 'holding', 'group', 'capital', 'financial', 'finance', 'bank', 'banking', 'securities', 'investment',
           'investments', 'investors', 'advisors', 'advisers', 'advisory', 'management', 'partners', 'associates', 'services',
           'service', 'distributors', 'income', 'recovery', 'underlying', 'access', 'client', 'clients', 'concierge', 'global',
           'international', 'national', 'general', 'limited', 'private', 'insurance', 'institutional', 'class', 'sweep', 'daily',
           'municipal', 'tax', 'free', 'new', 'american', 'us', 'usa', 'united', 'states', 'enterprises', 'industries',
           'technologies', 'systems', 'solutions', 'consulting', 'media', 'realty', 'real', 'estate', 'properties', 'property',
           'development', 'equity', 'asset', 'assets', 'fiduciary', 'family', 'charitable', 'revocable', 'irrevocable', 'living',
           'operating', 'master', 'feeder', 'offshore', 'domestic', 'opportunities', 'secondary', 'mutual', 'money', 'market',
           'cash', 'reserve', 'reserves', 'treasury', 'bond', 'bonds', 'stock', 'common', 'preferred', 'and', '&', 'of', 'the',
           'for', 'de', 'la', 'le', 'du', 'brokerage', 'clearing', 'custody', 'custodian', 'deposit', 'savings', 'loan', 'credit',
           'mortgage', 'travel', 'related', 'office', 'law', 'legal', 'firm', 'llc.', 'name', 'account', 'accounts', 'agent', 'nominee',
           'liability', 'ii', 'iii', 'iv', 'v', 'vi', 'est', 'annual', 'restitution', 'consumer', 'corporation', 'professional'}
# Leading words that glue a sentence or label onto a name ("copyright of", "valuation statement for").
GLUE = LEADING | {'copyright', 'statement', 'statements', 'affiliates', 'affiliate', 'liability', 'valuation', 'member', 'members',
                  'subsidiary', 'subsidiaries', 'division', 'client', 'account', 'name', 'c', 'o', 'cc', 'via', 'attn', 'att',
                  'care', 'payable', 'pay', 'order', 'beneficiary', 'custodian', 'agent', 'nominee', 'fbo', 'f', 'b', 'tod',
                  'ref', 'reference', 'invoice', 'bill', 'billed', 'acct', 'llc', 'inc', 'ltd', 'corp'}


def base_name(key):
    """Key without legal-form words at the end: "southern trust co inc" -> "southern trust"."""
    words = key.split()
    while len(words) > 1 and words[-1] in FORM_WORDS:
        words.pop()
    return ' '.join(words)


def consolidate(pages_of, kg_names):
    """{key: (status, target)}: status 'glue' / 'fragment' (target = the key it belongs to), 'generic',
    'kg' (target = KG entity name) or 'candidate'."""
    docs = {k: len(v) for k, v in pages_of.items()}
    status = {}
    kg_exact, kg_base = {}, defaultdict(set)
    for name in kg_names:
        prefix, _, suffix = name.rpartition(' ')
        key = name_key(prefix, suffix) if prefix and suffix.casefold().strip('.,') in {s.strip('.') for s in SUFFIXES} else None
        words = re.findall(r"[a-z0-9&]+", name.casefold().replace("'", ''))
        while words and words[0] in LEADING:
            words.pop(0)
        plain = ' '.join(words)
        kg_exact[key or plain] = name
        kg_base[base_name(key or plain)].add(name)
    by_tail = defaultdict(list)  # last word (the suffix) -> longer keys, for the fragment check
    for key in pages_of:
        by_tail[key.split()[-1]].append(key)
    by_length_tail = defaultdict(list)  # frequent keys by suffix word, for the OCR check
    for key in sorted(pages_of, key=lambda k: -docs[k]):
        if docs[key] >= 30:
            by_length_tail[(key.split()[-1],)].append(key)
    for key in sorted(pages_of, key=lambda k: -docs[k]):
        words = key.split()
        # Glue: leading label words on a more frequent name.
        for cut in range(1, len(words) - 1):
            tail = ' '.join(words[cut:])
            joined = words[cut - 1] in ('and', 'of', 'for', 'to', 'by', 'from', 'with', 'v', 'vs', 'at')  # "Jack Goldberger and the ..."
            if tail in pages_of and docs[tail] >= docs[key] and (joined or all(w in GLUE or re.search(r'\d', w) for w in words[:cut])):
                status[key] = ('glue', tail)
                break
        if key in status:
            continue
        if key in kg_exact:
            status[key] = ('kg', kg_exact[key])
            continue
        if len(base_name(key).split()) >= 2 and len(kg_base.get(base_name(key), ())) == 1:
            status[key] = ('kg', next(iter(kg_base[base_name(key)])))
            continue
        # OCR variant of a far more frequent name with the same legal form and digits
        # ("deutsche rank trust" / "deutsche bank trust"; never "crw 2007" / "crw 2009").
        if len(key) >= 10:
            limit = 2 if len(key) >= 16 else 1
            digits = re.findall(r'\d', key)
            for other in by_length_tail.get((words[-1],), ()):
                if docs[other] >= 10 * docs[key] and abs(len(other) - len(key)) <= limit \
                        and re.findall(r'\d', other) == digits and Levenshtein.distance(key, other, score_cutoff=limit) <= limit:
                    status[key] = ('ocr', other)
                    break
            if key in status:
                continue
        # Fragment: mostly written inside a longer name ending in the same words, on the same pages.
        mine = pages_of[key]
        best, share = None, 0.0
        for other in by_tail[words[-1]]:
            if other != key and other.endswith(' ' + key) and docs[other] >= 0.2 * docs[key]:
                overlap = len(mine & pages_of[other]) / len(mine)
                if overlap > share:
                    best, share = other, overlap
        if best and share >= 0.6:
            status[key] = ('fragment', best)
            continue
        if all(w in GENERIC or w.isdigit() for w in words):
            status[key] = ('generic', None)
            continue
        status[key] = ('candidate', None)
    return status


def scan(bounds):
    low, high, part = bounds
    path = MENTIONS.with_name(f'company_mentions.part{part}.db')
    path.unlink(missing_ok=True)
    corpus, rows, count = corpus_source.connect(), [], 0
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE mentions(key TEXT, spelling TEXT, page_id INTEGER)')
        pages = corpus.execute('SELECT page_id, text FROM pages WHERE page_id >= ? AND page_id < ?', (low, high)) if low is not None \
            else [(page_id, text) for page_id, _, _, text in supplement_pages()]  # build_supplement.py
        for page_id, text in pages:
            for key, spelling in extract(text):
                rows.append((key, spelling, page_id))
            if len(rows) > 50000:
                db.executemany('INSERT INTO mentions VALUES (?,?,?)', rows)
                count += len(rows)
                rows = []
        db.executemany('INSERT INTO mentions VALUES (?,?,?)', rows)
        db.commit()
    return str(path), count + len(rows)


def scan_corpus(args, start):
    with closing(corpus_source.connect()) as corpus:
        max_page = corpus.execute('SELECT max(page_id) FROM pages').fetchone()[0]
    if args.limit_pages:
        max_page = min(max_page, args.limit_pages)
    chunks = 64
    bounds = [(1 + i * max_page // chunks, 1 + (i + 1) * max_page // chunks, i) for i in range(chunks)]
    extra = {page_id: efta for page_id, efta, _, _ in supplement_pages()} if not args.limit_pages else {}
    if extra:
        bounds.append((None, None, chunks))  # text read off images, handwriting
    MENTIONS.parent.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(args.workers) as pool:
        parts = list(pool.map(scan, bounds))
    print(f'scanned {max_page} page ids in {time.time() - start:.0f}s, {sum(n for _, n in parts)} mentions')

    page_doc, doc_names = page_map()
    doc_names = list(doc_names)
    doc_index = {name: i for i, name in enumerate(doc_names)}
    for efta in extra.values():
        if efta not in doc_index:
            doc_index[efta] = len(doc_names)
            doc_names.append(efta)
    partial = MENTIONS.with_suffix('.partial.db')
    partial.unlink(missing_ok=True)
    docs, pages, spellings, pages_of = defaultdict(set), Counter(), defaultdict(Counter), defaultdict(set)
    with closing(sqlite3.connect(partial)) as db:
        db.execute('CREATE TABLE mentions(key TEXT, spelling TEXT, page_id INTEGER, efta TEXT)')
        for path, _ in parts:
            with closing(sqlite3.connect(path)) as part:
                rows = []
                for key, spelling, page_id in part.execute('SELECT key, spelling, page_id FROM mentions'):
                    doc = doc_index[extra[page_id]] if page_id >= SUPPLEMENT_BASE else \
                        int(page_doc[page_id]) if page_id < len(page_doc) else -1
                    docs[key].add(doc)
                    pages[key] += 1
                    pages_of[key].add(page_id)
                    spellings[key][spelling] += 1
                    rows.append((key, spelling, page_id, doc_names[doc] if doc >= 0 else None))
            db.executemany('INSERT INTO mentions VALUES (?,?,?,?)', rows)
            Path(path).unlink()
        db.execute('CREATE INDEX mentions_key ON mentions(key)')
        db.commit()
    partial.replace(MENTIONS)
    return docs, pages, spellings, pages_of


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--limit-pages', type=int, default=0, help='scan only the first N page IDs (testing)')
    parser.add_argument('--min-docs', type=int, default=3)
    parser.add_argument('--reuse', action='store_true', help='reuse output/.build/company_mentions.db (no rescan)')
    args = parser.parse_args()
    start = time.time()
    if args.reuse and MENTIONS.exists():
        docs, pages, spellings, pages_of = defaultdict(set), Counter(), defaultdict(Counter), defaultdict(set)
        with closing(sqlite3.connect(f'file:{MENTIONS}?mode=ro', uri=True)) as db:
            for key, spelling, page_id, efta in db.execute('SELECT key, spelling, page_id, efta FROM mentions'):
                docs[key].add(efta)
                pages[key] += 1
                spellings[key][spelling] += 1
                pages_of[key].add(page_id)
    else:
        docs, pages, spellings, pages_of = scan_corpus(args, start)
    entities = json.loads((OUT / 'normalized_entities.json').read_text())
    kg_names = [e['name'] for e in entities if e['entity_type'] in ('organization', 'shell_company', 'property', 'aircraft')
                and not re.match(r'(HeaderLabel|Email)_', e['name'])]
    small = {k: v for k, v in pages_of.items() if len(docs[k]) >= args.min_docs}
    status = consolidate(small, kg_names)
    # Folded keys add their documents to the key they belong to.
    def root(key, seen=()):
        kind, target = status.get(key, ('candidate', None))
        return root(target, (*seen, key)) if kind in ('glue', 'fragment', 'ocr') and target not in seen else key
    total_docs = defaultdict(set)
    for key in small:
        total_docs[root(key)] |= docs[key]
    kept = sorted(small, key=lambda k: -len(total_docs.get(k, docs[k])))
    with open(CANDIDATES, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['key', 'status', 'target', 'documents', 'documents_with_folded', 'mentions', 'top_spelling', 'spellings'])
        for key in kept:
            kind, target = status[key]
            top = spellings[key].most_common(5)
            writer.writerow([key, kind, target or '', len(docs[key]), len(total_docs.get(key, ())) or '', pages[key], top[0][0],
                             ' | '.join(f'{s} ({n})' for s, n in top)])
    print(f'{len(docs)} keys, {len(kept)} in >= {args.min_docs} documents: {Counter(k for k, _ in status.values())} '
          f'-> {CANDIDATES.relative_to(ROOT)} ({time.time() - start:.0f}s)')


if __name__ == '__main__':
    main()
