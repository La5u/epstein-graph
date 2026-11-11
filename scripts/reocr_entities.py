"""PRIVATE, local-only: re-read the pages behind short company names, turned pages included.

    .venv/bin/python -B scripts/reocr_entities.py [--docs 3] [--workers 3] [--dpi 300] [--names "E, Inc." ...]
                                                  [--only short|near] [--rare 20] [--factor 5]

A company node whose name is very short ("E, Inc.", "Mon, Inc.") or starts
with a bare letter ("E Management New York LLC") may be an OCR cut-off of a
longer name: "E, Inc." was "JEGE, Inc." on a page turned 90 degrees. So may a
name seen in few documents that is a letter or two off a much more common one
with the same legal form ("SIX Designs, LLC", 4 documents, and "SLK Designs
LLC", 370): DOJ's text misreads whole words of clean prints. For these the
verdicts only count names that near. For each
such node (or each --names spelling), up to --docs documents are chosen from
output/.build/company_mentions.db, which records the page of every mention, and
that page is read from the official justice.gov PDF: DOJ's text layer and
Tesseract 5 --psm 6 at 0, 90, 270 and 180 degrees. The turned reading is used
when reocr_rotation.clearly_better says it reads better than the upright one.

Per page the verdict compares the best reading with DOJ's page text:
  longer_name  the reading has another company node whose name ends in this one
               ("jege inc" for "e inc"), starts with it or is a near spelling,
               DOJ's text does not, and the reading has this name fewer times
  same         the reading still has this name (it is what the scan shows)
  misread      on the line matching the mention, the reading has another company
               node with the same legal form instead ("JEGE, LLC" for "MGR, LLC");
               for a name near a common one, the near name on that line or, as
               for longer_name, on the page
  unclear      neither (illegible or redacted)
The lines around the mention in each reading are kept for the reviewer.
Nothing is merged: suggestions go to config/company_review.json after a person
has checked them. Writes data/private/ocr_review/entity_reocr.json (page lines
with raw text, never part of output/); page readings are cached in
data/private/reocr_entities/ so reruns skip OCR. PDFs are shared with the other
reocr_* scripts in data/external/reocr/.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from pathlib import Path
from queue import Queue

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from build_companies import LEGAL, base_of, normal_name
import corpus as corpus_source
from corpus import document_urls
import reocr_full
from reocr_rotation import ANGLES, clearly_better, dictionary, word_score
from supplement import SUPPLEMENT_BASE

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'data/external/reocr'
CACHE = ROOT / 'data/private/reocr_entities'
OUT = ROOT / 'data/private/ocr_review/entity_reocr.json'
MENTIONS = ROOT / 'output/.build/company_mentions.db'
NODES = ROOT / 'output/company_nodes.json'
WINDOW = 2  # lines either side of the mention
WORDS = None
DPI = 300


def suspect_reason(name):
    """'short' / 'bare_letter' for names that may be OCR cut-offs, else None."""
    base = base_of(normal_name(name))
    if len(re.sub(r'[^a-z0-9]', '', base)) <= 3:
        return 'short'
    # "E Management New York LLC", not initials ("J. Epstein"), "D'Agostino", "M-Related" or "K & F".
    m = re.match(r'(?:the\s+)?([A-Za-z])\s+(\S+)', name, re.I)
    if m and m.group(2).casefold() not in ('&', 'and') and len(m.group(2)) > 1:
        return 'bare_letter'
    return None


def squeeze(key):
    return re.sub(r'[^a-z0-9]', '', base_of(key))


def near(key, other):
    """Could OCR have read `other` as `key`? One or two letters apart, same legal form ("apo 02 trust", "apo2 trust")."""
    if other == key or legal_form(key) != legal_form(other):
        return False
    a, b = squeeze(key), squeeze(other)
    return len(a) >= 4 and Levenshtein.distance(a, b) <= 2 and fuzz.ratio(a, b) >= 80


def near_common(nodes, rare=20, factor=5, common=20):
    """{id: id of the most common node} for names in at most `rare` documents near one in `factor` times as many."""
    by_form = defaultdict(list)
    for n in nodes:
        key = normal_name(n['name'])
        by_form[legal_form(key)].append((n['metadata'].get('document_count', 0), key, n['id']))
    out = {}
    for group in by_form.values():
        group.sort(reverse=True)  # most documents first
        for count, key, identity in group:
            if count <= rare:
                other = next((o for c, k, o in group if c >= max(common, factor * count) and near(key, k)), None)
                if other:
                    out[identity] = other
    return out


def node_keys(node):
    return {normal_name(node['name'])} | set(node['metadata'].get('spellings', []))


def company_index(nodes):
    """{phrase: {normalized key: node id}} over every company node's name and folded spellings.

    The phrase is the key itself or its name without the legal form (from 4 characters),
    since OCR often garbles the form ("JEGE NG" for "JEGE INC").
    """
    index = defaultdict(dict)
    for node in nodes:
        for key in node_keys(node):
            index[key].setdefault(key, node['id'])
            if len(base := base_of(key)) >= 4:
                index[base].setdefault(key, node['id'])
    return index


def legal_form(key):
    return key[len(base_of(key)):].strip()


def related(key, other):
    """Could `other` be the full name that OCR cut down to `key`?"""
    if other == key or len(other) <= len(key):
        return False
    if legal_form(key) != legal_form(other):  # JEGE, Inc. and JEGE, LLC stay apart
        return False
    if other.endswith(key):  # "jege inc" for "e inc": the left of the name was lost
        return True
    if base_of(other).startswith(base_of(key)):  # "mondo inc" for "mon inc": the right was lost
        return True
    return len(base_of(key)) >= 4 and fuzz.ratio(key, other) >= 80


def count(keys, text):
    """How often the normalized keys occur, as whole words or run together ("SFLLLC"), in the text's lines."""
    lines = [f' {normal_name(line)} ' for line in (text or '').splitlines()]
    return sum(line.count(f' {k} ') + (line.count(f' {k.replace(" ", "")} ') if ' ' in k else 0)
               for k in keys for line in lines)


def mention_line(text, keys):
    """[line before, the corpus line holding one of the keys (the mention), line after], or None."""
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if any(f' {k} ' in f' {normal_name(line)} ' for k in keys):
            return [lines[i - 1] if i else '', line, lines[i + 1] if i + 1 < len(lines) else '']
    return None


def similarity(target, line):
    if not target or not line:
        return 0
    # A short line (one letter of a turned page) matches anywhere in the target: weigh by length.
    return fuzz.partial_ratio(target.casefold(), line.casefold()) * min(1, len(line) / len(target))


def window(text, context):
    """Lines around the reading's closest match to the corpus mention line, which is at [WINDOW].

    The neighbouring corpus lines count half: a short mention ("E, INC.") alone
    matches any line ending in ", Inc.".
    """
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    if not lines or not context[1]:
        return []
    before, target, after = context
    scores = [similarity(target, line) + (similarity(before, lines[i - 1] if i else '')
                                          + similarity(after, lines[i + 1] if i + 1 < len(lines) else '')) / 2
              for i, line in enumerate(lines)]
    i = max(range(len(lines)), key=scores.__getitem__)
    return [''] * max(0, WINDOW - i) + lines[max(0, i - WINDOW):i + WINDOW + 1]


def same_form(key, other):
    return other != key and legal_form(key) == legal_form(other)


def candidates(keys, lines, index, own, match=related):
    """{key: node id} of other company nodes in the lines that this name may be cut from (or `match`)."""
    out = {}
    for line in lines:
        words = normal_name(line).split()
        for i in range(len(words)):
            for j in range(i + 1, min(len(words), i + 8) + 1):
                phrase = ' '.join(words[i:j])
                for key, node in index.get(phrase, {}).items():
                    if key != phrase and j < len(words) and words[j] in LEGAL:
                        continue  # the bare name followed by another, legible legal form
                    if node != own and any(match(k, key) for k in keys):
                        out[key] = node
    return out


def verdict(keys, reading, corpus_text, index, own, line='', corpus_line='', near_only=False):
    """Compare the whole page, not lines: on a turned page the lines don't line up.

    A longer name counts when the reading has it, DOJ's page text does not, and
    the reading has the short name fewer times than DOJ's text. A whole-word
    misread ("MOE, Inc." for "JEGE, Inc.") is only looked for on the reading's
    `line` matching the mention: any company there with the same legal form
    that DOJ's mention line doesn't already have. With `near_only` both only
    count names a letter or two off this one, and both are misreads.
    """
    longer_match, misread_match = (near, near) if near_only else (related, same_form)
    page = (reading or '').splitlines()
    longer = candidates(keys, page, index, own, longer_match)
    known = candidates(keys, (corpus_text or '').splitlines(), index, own, longer_match)
    longer = {k: node for k, node in longer.items() if node not in known.values()}
    short, before = count(keys, reading), count(keys, corpus_text)
    if longer and short < before:
        return 'misread' if near_only else 'longer_name', longer
    if short:
        return 'same', longer
    known = candidates(keys, [corpus_line], index, own, misread_match)
    other = {k: node for k, node in candidates(keys, [line], index, own, misread_match).items()
             if node not in known.values()}
    return ('misread' if other else 'unclear'), other


def best_angle(scores):
    """The turned reading only when it is clearly better than the upright one."""
    best = max(ANGLES, key=lambda a: scores[a]['score'])
    return best if best and clearly_better(scores[best], scores[0]) else 0


def read_page(job):
    """{'text_layer': str, 'tesseract': {angle: str}, 'scores': {angle: score}} (runs in a worker)."""
    global WORDS
    pdf, page = job  # page is 0-based
    cached = CACHE / f'{Path(pdf).stem}_{page}_{DPI}.json'
    if cached.exists():
        return json.loads(cached.read_text())
    if WORDS is None:
        WORDS = dictionary()
    out = {'text_layer': subprocess.run(['pdftotext', '-layout', '-f', str(page + 1), '-l', str(page + 1), pdf, '-'],
                                        capture_output=True, text=True, timeout=90).stdout, 'tesseract': {}}
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['pdftoppm', '-r', str(DPI), '-f', str(page + 1), '-l', str(page + 1), '-gray', '-singlefile',
                        pdf, f'{tmp}/p'], check=True, capture_output=True, timeout=90)
        from PIL import Image
        with Image.open(f'{tmp}/p.pgm') as image:
            for angle in ANGLES:
                path = f'{tmp}/r.png'
                (image.rotate(angle, expand=True) if angle else image).save(path)
                out['tesseract'][angle] = subprocess.run(
                    ['tesseract', path, '-', '--psm', '6'], capture_output=True, text=True, timeout=120, check=True,
                    env={**os.environ, 'OMP_THREAD_LIMIT': '1'}).stdout
    out['scores'] = {a: word_score(t, WORDS) for a, t in out['tesseract'].items()}
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=CACHE)
    with os.fdopen(fd, 'w') as stream:
        json.dump(out, stream)
    os.replace(temporary, cached)
    return json.loads(cached.read_text())  # angles as strings, like a cache hit


def check(reading, keys, corpus_line, corpus_text, index, own, near_only=False):
    scores = {int(a): s for a, s in reading['scores'].items()}
    texts = {int(a): t for a, t in reading['tesseract'].items()}
    angle = best_angle(scores)
    lines = {'text_layer': window(reading['text_layer'], corpus_line), 'tesseract': window(texts[0], corpus_line)}
    if angle:
        lines[f'tesseract_{angle}'] = window(texts[angle], corpus_line)
    middle = lines[f'tesseract_{angle}' if angle else 'tesseract'][WINDOW:WINDOW + 1]
    result, longer = verdict(keys, texts[angle], corpus_text, index, own, ''.join(middle), corpus_line[1],
                              near_only)
    return {'best_angle': angle, 'verdict': result, 'longer': sorted(longer), 'longer_ids': sorted(set(longer.values())),
            'lines': lines}


def suggestion(checks):
    """The name to review, when more pages read it than read the short name or any other name."""
    votes = Counter(i for c in checks if c['verdict'] in ('longer_name', 'misread') for i in c['longer_ids'])
    same = sum(c['verdict'] == 'same' for c in checks)
    (top, n), *rest = votes.most_common(2) or [(None, 0)]
    return top if n > same and not (rest and rest[0][1] == n) else None


def choose_pages(db, keys, limit, cached):
    """[(efta, page, spelling)]: one mention page in each of up to `limit` documents, cached PDFs first."""
    rows = db.execute(f'SELECT efta, spelling, MIN(page_id) FROM mentions WHERE key IN ({",".join("?" * len(keys))}) '
                      f'AND efta IS NOT NULL AND page_id < ? GROUP BY efta', (*sorted(keys), SUPPLEMENT_BASE)).fetchall()
    rows.sort(key=lambda r: (r[0] not in cached, r[0]))
    return [(efta, page_id, spelling) for efta, spelling, page_id in rows[:limit]]


def main():
    global DPI
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--docs', type=int, default=3, help='documents read per company')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--dpi', type=int, default=300)
    parser.add_argument('--pause', type=float, default=0.5, help='seconds between downloads')
    parser.add_argument('--names', nargs='+', help='check these spellings instead of the suspect nodes')
    parser.add_argument('--only', choices=('short', 'near'), help='only short names, or only names near a common one')
    parser.add_argument('--rare', type=int, default=20, help='near names: at most this many documents')
    parser.add_argument('--factor', type=int, default=5, help='near names: the common one in this many times as many')
    args = parser.parse_args()
    DPI = args.dpi
    os.umask(0o077)
    nodes = json.loads(NODES.read_text())
    index = company_index(nodes)
    if args.names:
        named = {normal_name(n['name']): n['id'] for n in nodes}  # not merged spellings: "E, Inc." is in JEGE's
        suspects = [{'id': named.get(normal_name(n)), 'name': n, 'reason': 'requested', 'keys': {normal_name(n)}}
                    for n in args.names]
    else:
        suspects = [] if args.only == 'near' else [
            {'id': n['id'], 'name': n['name'], 'reason': r, 'keys': node_keys(n)}
            for n in nodes if (r := suspect_reason(n['name']))]
        common = {} if args.only == 'short' else near_common(nodes, args.rare, args.factor)
        suspects += [{'id': n['id'], 'name': n['name'], 'reason': 'near_common', 'keys': node_keys(n),
                      'common': common[n['id']]}
                     for n in nodes if n['id'] in common and not any(s['id'] == n['id'] for s in suspects)]
    print(f'{len(suspects)} company names to check', flush=True)

    WORK.mkdir(parents=True, exist_ok=True)
    cached = {p.stem for p in WORK.glob('EFTA*.pdf')}
    corpus = corpus_source.connect()
    jobs = defaultdict(list)  # (efta, page) -> [(suspect index, spelling, corpus line)]
    with closing(sqlite3.connect(f'file:{MENTIONS}?mode=ro', uri=True)) as db:
        for n, suspect in enumerate(suspects):
            for efta, page_id, spelling in choose_pages(db, suspect['keys'], args.docs, cached):
                row = corpus.execute('SELECT page, text FROM pages WHERE page_id=?', (page_id,)).fetchone()
                if row:
                    jobs[(efta, row[0])].append((n, spelling, mention_line(row[1], suspect['keys']) or ['', spelling, ''], row[1]))
    urls = {e: u for e, u in document_urls({e for e, _ in jobs}).items() if u.startswith('https://www.justice.gov/')}
    todo = sorted({e for e, _ in jobs if e in urls}, key=lambda e: (e not in cached, e))
    print(f'{len(jobs)} pages in {len(todo)} documents ({sum(e in cached for e in todo)} already downloaded)', flush=True)

    ready = Queue(maxsize=args.workers * 4)
    threading.Thread(target=reocr_full.downloader, args=(todo, urls, ready, args.pause), daemon=True).start()
    pages_of = defaultdict(list)
    for job in jobs:
        pages_of[job[0]].append(job)
    futures, failed = {}, 0
    results = defaultdict(list)
    with ProcessPoolExecutor(args.workers) as pool:
        while (item := ready.get()) is not None:
            if isinstance(item, Exception):
                raise RuntimeError('downloader failed') from item
            efta, ok = item
            failed += not ok
            for job in pages_of[efta] if ok else ():
                futures[job] = pool.submit(read_page, (str(WORK / f'{efta}.pdf'), job[1]))
        print(f'downloads done, {failed} failed', flush=True)
        for done, (job, future) in enumerate(futures.items(), 1):
            try:
                reading = future.result()
            except Exception as error:  # never log stderr: it may hold page text
                print(f'{job[0]} page {job[1]}: {type(error).__name__}', flush=True)
                continue
            for n, spelling, corpus_line, corpus_text in jobs[job]:
                suspect = suspects[n]
                results[n].append({'efta_number': job[0], 'page': job[1], 'pdf_url': urls[job[0]], 'spelling': spelling,
                                   'corpus_lines': corpus_line,
                                   **check(reading, suspect['keys'], corpus_line, corpus_text, index, suspect['id'],
                                         suspect['reason'] == 'near_common')})
            if done % 25 == 0:
                print(f'{done}/{len(futures)} pages read', flush=True)
    labels = {n['id']: n['name'] for n in nodes}
    report = []
    for n, suspect in enumerate(suspects):
        checks = results.get(n, [])
        target = suggestion(checks)
        report.append({'id': suspect['id'], 'name': suspect['name'], 'reason': suspect['reason'],
                       'near_common': labels.get(suspect.get('common')),
                       'verdicts': dict(Counter(c['verdict'] for c in checks)),
                       'suggested_merge_into': labels.get(target), 'checks': checks})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1, ensure_ascii=False) + '\n')
    os.chmod(OUT, 0o600)
    checks = [c for r in report for c in r['checks']]
    print(json.dumps({'names': len(report), 'names_read': sum(bool(r['checks']) for r in report), 'pages': len(checks),
                      'turned_pages': sum(bool(c['best_angle']) for c in checks),
                      'verdicts': dict(Counter(c['verdict'] for c in checks)),
                      'suggestions': {r['name']: r['suggested_merge_into'] for r in report if r['suggested_merge_into']}},
                     ensure_ascii=False, indent=1))
    print(f'-> {OUT.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
