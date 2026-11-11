"""PRIVATE, local-only: re-OCR the header lines behind suspicious name-only participants.

    .venv/bin/python scripts/reocr_participants.py [--docs 3] [--workers 3] [--dpi 300]

A participant is a From/To/Cc display name without a usable address. Short or
odd labels ("ail.com", "Jeffre", "lain Macara") may be OCR cut-offs of a real
name or address. For each viewer participant whose label looks truncated, up to
--docs documents with that header are chosen; the corpus page holding the label
is read from the official justice.gov PDF three ways (DOJ text layer,
Tesseract 5 --psm 6, RapidOCR) and the header line each engine sees is
recorded next to the corpus line, so a reviewer can tell whether the full
name or address is legible on the scan.

Writes ONLY data/private/ocr_review/participant_reocr.json (raw names; never
part of output/). PDFs are cached in data/external/reocr/ with reocr_full.py's.
Nothing is merged or changed in the graph.
"""
import argparse
import json
import os
import re
import subprocess
import tempfile
import threading
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from pathlib import Path
from queue import Queue

from rapidfuzz import fuzz

from build_communications import Builder, label_quality, parse_recipient, parse_sender
from build_contacts import contact_key
import corpus as corpus_source
import emails as email_source
from corpus import document_urls
import reocr_full

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'data/external/reocr'
OUT = ROOT / 'data/private/ocr_review/participant_reocr.json'
HEADER = re.compile(r'^\s*(from|to|cc|sent|subject|date)\s*[:;]', re.I)


def looks_truncated(label):
    """Heuristic: labels worth checking on the scan (cut-off names/addresses, garbles)."""
    words = label.split()
    if re.search(r'\w\.(com|net|org|edu)\b', label, re.I):
        return True  # domain fragment: "ail.com"
    if len(words) == 1 and (label.islower() or len(label) <= 4):
        return True  # "ion", "Jes", "mwalkermd"
    if words[0][0].islower() or len(words[0]) <= 2:
        return True  # "lain Macara", "iew our Di ital Itinera", "An Glass"
    if len(words) >= 2 and len(words[-1].strip('.')) <= 2:
        return True  # "James Cti", "Kira D", "Mhahasi Srubelco la"
    return label_quality(label) != 'ok' and label_quality(label) != 'group_or_role'


def participant_documents(key, wanted):
    """{participant_id: {raw_spelling: {efta}}} for header names in the email headers."""
    builder, found = Builder(key, []), defaultdict(lambda: defaultdict(set))

    def add(name, efta):
        identity = builder.endpoint(name, None)
        if identity in wanted:
            found[identity][' '.join(name.split())].add(efta)

    with closing(email_source.headers()) as db:
        for efta, name, address, to_raw, cc_raw in db.execute(
                'SELECT doc_id,sender_name,sender_email,to_json,cc_json FROM headers WHERE noise=0'):
            name, address = parse_sender(name, address)
            if isinstance(name, str) and not address:
                add(name, efta)
            for raw_value in (to_raw, cc_raw):
                try:
                    items = json.loads(raw_value or '[]')
                except (ValueError, TypeError):
                    continue
                for item in items if isinstance(items, list) else ():
                    for recipient, recipient_address in parse_recipient(item):
                        if recipient and not recipient_address:
                            add(recipient, efta)
    return found


def find_line(text, spelling):
    """(index, line) of the corpus line holding the spelling, preferring header lines."""
    lines = (text or '').splitlines()
    needle = spelling.casefold()
    hits = [(i, line) for i, line in enumerate(lines) if needle in ' '.join(line.split()).casefold()]
    hits.sort(key=lambda h: not HEADER.match(h[1]))
    return hits[0] if hits else (None, None)


def rapid_rows(lines):
    """Join RapidOCR boxes into visual rows (it returns one box per text run)."""
    boxes = sorted(((sum(p[1] for p in box) / 4, min(p[0] for p in box), text, max(p[1] for p in box) - min(p[1] for p in box))
                    for box, text, _ in lines or ()))
    rows, current, row_y = [], [], None
    for y, x, text, height in boxes:
        if row_y is not None and abs(y - row_y) > max(height, 10) * 0.6:
            rows.append(current)
            current = []
        if not current:
            row_y = y
        current.append((x, text))
    if current:
        rows.append(current)
    return [' '.join(t for _, t in sorted(row)) for row in rows]


def read_page(job):
    """Text layer, Tesseract and RapidOCR lines of one page (runs in a worker)."""
    pdf, page = job  # page is 1-based
    if reocr_full._rapid is None:
        reocr_full._rapid = reocr_full.rapid_engine()
    out = {'text_layer': subprocess.run(['pdftotext', '-layout', '-f', str(page), '-l', str(page), pdf, '-'],
                                        capture_output=True, text=True).stdout.splitlines()}
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['pdftoppm', '-r', str(DPI), '-f', str(page), '-l', str(page), '-png', pdf, f'{tmp}/p'], check=True)
        png = str(next(Path(tmp).glob('p*.png')))
        out['tesseract'] = subprocess.run(['tesseract', png, '-', '--psm', '6'], capture_output=True, text=True,
                                          env={**os.environ, 'OMP_THREAD_LIMIT': '1'}).stdout.splitlines()
        out['rapidocr'] = rapid_rows(reocr_full._rapid(png)[0])
    return out


DPI = 300


def best_line(lines, target):
    """Closest engine line to the corpus header line, with one line of context each side."""
    lines = [line.strip() for line in lines if line.strip()]
    if not lines:
        return None
    scores = [fuzz.partial_ratio(target.casefold(), line.casefold()) + fuzz.ratio(target.casefold(), line.casefold()) / 4
              for line in lines]
    i = max(range(len(lines)), key=scores.__getitem__)
    return {'line': lines[i], 'before': lines[i - 1] if i else '', 'after': lines[i + 1] if i + 1 < len(lines) else ''}


def main():
    global DPI
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--docs', type=int, default=3, help='documents checked per participant')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--dpi', type=int, default=300)
    parser.add_argument('--pause', type=float, default=0.5, help='seconds between downloads')
    args = parser.parse_args()
    DPI = args.dpi
    key = contact_key()
    lookup = json.loads((ROOT / 'data/private/contact_lookup.json').read_text())
    shown = [n['id'] for n in json.loads((ROOT / 'output/communication_nodes.json').read_text()) if n['entity_type'] == 'participant']
    labels = {i: lookup[i]['value'] for i in shown if i in lookup}
    suspects = {i: label for i, label in labels.items() if looks_truncated(label)}
    print(f'{len(shown)} participants shown, {len(suspects)} look truncated', flush=True)

    spellings = participant_documents(key, set(suspects))
    urls = document_urls({e for by in spellings.values() for docs in by.values() for e in docs})
    corpus = corpus_source.connect()
    jobs = {}  # (efta, page) -> [(participant, spelling, corpus line)]
    for identity in sorted(suspects, key=suspects.get):
        picked = 0
        pairs = sorted(((s, e) for s, docs in spellings[identity].items() for e in docs if e in urls),
                       key=lambda p: (-len(spellings[identity][p[0]]), p[1]))
        seen_docs = set()
        for spelling, efta in pairs:
            if picked >= args.docs or efta in seen_docs:
                continue
            for page, text in corpus.execute('SELECT page, text FROM pages WHERE doc_id=? ORDER BY page', (efta,)):
                _, line = find_line(text, spelling)
                if line:
                    jobs.setdefault((efta, page + 1), []).append((identity, spelling, line.strip()))
                    seen_docs.add(efta)
                    picked += 1
                    break
    todo = sorted({e for e, _ in jobs})
    print(f'{len(jobs)} pages in {len(todo)} documents to read', flush=True)

    WORK.mkdir(parents=True, exist_ok=True)
    # Cached PDFs first, so OCR starts at once while the rest download.
    todo.sort(key=lambda e: not (WORK / f'{e}.pdf').exists())
    ready = Queue(maxsize=args.workers * 4)
    threading.Thread(target=reocr_full.downloader, args=(todo, urls, ready, args.pause), daemon=True).start()
    pages_of = defaultdict(list)
    for job in jobs:
        pages_of[job[0]].append(job)
    results, futures, failed = defaultdict(list), {}, 0
    with ProcessPoolExecutor(args.workers) as pool:
        while (item := ready.get()) is not None:
            if isinstance(item, Exception):
                raise RuntimeError('downloader failed') from item
            efta, ok = item
            failed += not ok
            for job in pages_of[efta] if ok else ():
                futures[job] = pool.submit(read_page, (str(WORK / f'{efta}.pdf'), job[1]))
        print(f'downloads done, {failed} failed', flush=True)
        for n, (job, future) in enumerate(futures.items(), 1):
            texts = future.result()
            for identity, spelling, corpus_line in jobs[job]:
                results[identity].append({'efta_number': job[0], 'page': job[1], 'pdf_url': urls[job[0]], 'spelling': spelling,
                                          'corpus_line': corpus_line,
                                          **{name: best_line(texts[name], corpus_line) for name in ('text_layer', 'tesseract', 'rapidocr')}})
            if n % 25 == 0:
                print(f'{n}/{len(futures)} pages read', flush=True)
    report = [{'id': i, 'label': suspects[i], 'checks': results.get(i, [])} for i in sorted(suspects, key=suspects.get)]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1, ensure_ascii=False) + '\n')
    os.chmod(OUT, 0o600)
    print(f'{sum(bool(r["checks"]) for r in report)}/{len(report)} participants checked -> {OUT.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
