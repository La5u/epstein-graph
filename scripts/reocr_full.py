"""Two-engine re-OCR of every OCR-variant email merge (one document per variant).

    .venv/bin/python scripts/reocr_full.py [--workers 3] [--dpi 300] [--limit N]

Extends reocr_pilot.py from a 60-document sample to full variant coverage: for
each address merged by `ocr_variant_of_more_frequent_address`, one document
whose metadata produced it is chosen (documents already chosen for another
variant are reused). The official PDF is downloaded from justice.gov and up to
--max-pages pages (default 8) are read three ways, pages whose corpus text holds
the variant first, since addresses recur in quoted replies further down a thread:
  text_layer  DOJ's embedded text (pdftotext)
  tesseract   Tesseract 5, LSTM, --psm 6
  rapidocr    RapidOCR (PaddleOCR detection + recognition models on ONNX), an
              independent engine with different models and failure modes.
Each engine gets the pilot's verdict (confirmed / same_garble / neither) per
variant; output/reocr_full.json reports per-engine counts and how often the
two OCR engines agree. Only HMAC IDs are written, never raw addresses.

Resumable: per-document results append to data/external/reocr/full_results.jsonl
(git-ignored with the PDFs); rerunning skips finished documents. Downloads are
sequential with a short pause, separate from the OCR worker pool.
"""
import argparse
import json
import os
import random
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from queue import Queue

from build_communications import valid_email
from build_contacts import contact_id, contact_key
import corpus as corpus_source
from corpus import document_urls
from reocr_pilot import EMAIL, addresses, age_verified_cookies

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'data/external/reocr'
RESULTS = WORK / 'full_results.jsonl'
UA = 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36'
ENGINES = ('text_layer', 'tesseract', 'rapidocr')


def ocr_text(text):
    # RapidOCR splits on spaces around the at-sign ("name @ gmail.com").
    return re.sub(r'\s*@\s*', '@', text or '')


def choose_documents(db, corpus, key, seed=7):
    """{efta: {variant_id}} covering each OCR variant once; plus {efta: [pages]}."""
    variants = dict(db.execute("SELECT member_id, canonical_id FROM identity_map WHERE rule='ocr_variant_of_more_frequent_address'"))
    docs_of = defaultdict(set)
    for efta, source, target in db.execute("SELECT efta_number, source_id, target_id FROM evidence WHERE source_db='email_metadata.db' AND efta_number IS NOT NULL"):
        for member in (source, target):
            if member in variants:
                docs_of[member].add(efta)
    urls = document_urls({e for docs in docs_of.values() for e in docs})
    official = {e for e, u in urls.items() if u.startswith('https://www.justice.gov/')}
    rng = random.Random(seed)
    chosen = defaultdict(set)
    for member in sorted(variants):
        candidates = sorted(docs_of[member] & official)
        if not candidates:
            continue
        reuse = [e for e in candidates if e in chosen]
        chosen[rng.choice(reuse or candidates)].add(member)
    # Variants an already-chosen document also contains are checked there too.
    for member, docs in docs_of.items():
        for efta in docs & chosen.keys():
            chosen[efta].add(member)
    pages = {}
    for efta, members in chosen.items():
        hits = [n for n, text in corpus.execute('SELECT page, text FROM pages WHERE doc_id=? ORDER BY page', (efta,))
                if members & {contact_id(v, 'email', key) for raw in EMAIL.findall(text or '') if (v := valid_email(raw.strip('.,;:<>()[]')))}]
        pages[efta] = hits
    return variants, chosen, pages, urls


_rapid = None


def rapid_engine(threads=2):
    """RapidOCR with bounded threads (ONNX defaults to every core per process,
    which oversubscribes a worker pool), no angle classifier (scans are upright)
    and the detector capped at 2400 px (unbounded it keeps full 300 dpi pages;
    2400 read identical addresses on test pages with ~40% less memory)."""
    import onnxruntime
    import rapidocr_onnxruntime.utils

    def options():
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads, opts.inter_op_num_threads = threads, 1
        return opts
    rapidocr_onnxruntime.utils.SessionOptions = options  # no thread option upstream
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR(use_angle_cls=False)
    resize = engine.text_detector.preprocess_op[0]
    resize.limit_type, resize.limit_side_len = 'max', 2400
    return engine


def read_pdf(job):
    """OCR the requested pages of one PDF with both engines (runs in a worker)."""
    global _rapid
    pdf, hits, dpi, max_pages = job
    total = int(re.search(r'Pages:\s+(\d+)', subprocess.run(['pdfinfo', pdf], capture_output=True, text=True).stdout).group(1))
    # Pages whose corpus text holds a variant first, then the rest in order.
    order = [n + 1 for n in hits if n < total] + [n for n in range(1, total + 1) if n - 1 not in hits]
    if _rapid is None:
        _rapid = rapid_engine()
    out = {name: '' for name in ENGINES}
    with tempfile.TemporaryDirectory() as tmp:
        for first in order[:max_pages]:  # poppler pages are 1-based, corpus pages 0-based
            out['text_layer'] += subprocess.run(['pdftotext', '-f', str(first), '-l', str(first), pdf, '-'], capture_output=True, text=True).stdout
            subprocess.run(['pdftoppm', '-r', str(dpi), '-f', str(first), '-l', str(first), '-png', pdf, f'{tmp}/p{first}'], check=True)
            for png in sorted(Path(tmp).glob(f'p{first}*.png')):
                out['tesseract'] += subprocess.run(['tesseract', str(png), '-', '--psm', '6'], capture_output=True, text=True,
                                                   env={**os.environ, 'OMP_THREAD_LIMIT': '1'}).stdout
                lines, _ = _rapid(str(png))
                out['rapidocr'] += '\n'.join(line[1] for line in lines or []) + '\n'
    out['pages_read'] = sorted(order[:max_pages])
    return out


def verdict(member, family, ids):
    if member in ids:
        return 'same_garble'
    return 'confirmed' if (family - {member}) & ids else 'neither'


def downloader(todo, urls, ready, pause):
    cookie = None
    try:
        for efta in todo:
            pdf = WORK / f'{efta}.pdf'
            for attempt in range(2):
                if pdf.exists():
                    break
                if cookie is None:
                    cookie = age_verified_cookies(urls[efta])
                done = subprocess.run(['curl', '-sL', '--fail', '--max-time', '120', '-A', UA, '-H', f'Cookie: {cookie}', '-o', str(pdf), urls[efta]])
                if done.returncode or not pdf.exists() or pdf.read_bytes()[:4] != b'%PDF':
                    pdf.unlink(missing_ok=True)
                    cookie = None  # age-verification session may have expired; redo once
                time.sleep(pause)
            ready.put((efta, pdf.exists()))
    except Exception as error:
        ready.put(error)
    finally:
        ready.put(None)


def summarize(results, variants_total, docs_total):
    per_engine = {name: Counter(r['verdicts'][name] for r in results) for name in ENGINES}
    pair = Counter((r['verdicts']['tesseract'], r['verdicts']['rapidocr']) for r in results)
    agree = sum(n for (a, b), n in pair.items() if a == b)
    by_variant = {}
    for r in results:  # one verdict per variant: best evidence across its documents
        rank = {'confirmed': 2, 'neither': 1, 'same_garble': 0}
        best = max((r['verdicts']['tesseract'], r['verdicts']['rapidocr']), key=rank.get)
        if rank[best] > rank.get(by_variant.get(r['variant_id']), -1):
            by_variant[r['variant_id']] = best
    return {
        'variants_total': variants_total,
        'variants_checked': len(by_variant),
        'documents_with_variants': docs_total,
        'documents_read': len({r['efta_number'] for r in results}),
        'checks': len(results),
        'per_engine': {k: dict(v) for k, v in per_engine.items()},
        'tesseract_vs_rapidocr': {f'{a}/{b}': n for (a, b), n in sorted(pair.items())},
        'ocr_engines_agree': round(agree / len(results), 4) if results else None,
        'both_ocr_confirm': pair[('confirmed', 'confirmed')],
        'either_ocr_confirms': sum(n for (a, b), n in pair.items() if 'confirmed' in (a, b)),
        'variant_best_verdict': dict(Counter(by_variant.values())),
        'note': 'confirmed = that pass reads the merged-into address on the official scan and not the variant. '
                'same_garble = it reproduces the variant (damage is in the scan, or the address is genuinely different).',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--workers', type=int, default=3, help='OCR processes (~1.1 GB each, 2 ONNX threads + Tesseract)')
    parser.add_argument('--dpi', type=int, default=300)
    parser.add_argument('--max-pages', type=int, default=8, help='pages read per document (variant pages first)')
    parser.add_argument('--limit', type=int, help='only the first N chosen documents')
    parser.add_argument('--pause', type=float, default=0.5, help='seconds between downloads')
    args = parser.parse_args()
    key = contact_key()
    WORK.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(f'file:{ROOT / "output/communication_graph.db"}?mode=ro', uri=True)
    corpus = corpus_source.connect()
    variants, chosen, pages, urls = choose_documents(db, corpus, key)
    same_identity = defaultdict(set)
    for member, canonical in db.execute('SELECT member_id, canonical_id FROM identity_map'):
        same_identity[canonical].add(member)
    logged = defaultdict(set)
    for line in RESULTS.read_text().splitlines() if RESULTS.exists() else ():
        record = json.loads(line)
        logged[record['efta_number']].add(record['variant_id'])
    todo = sorted(chosen)[:args.limit] if args.limit else sorted(chosen)
    pending = [e for e in todo if not set(chosen[e]) <= logged[e]]
    print(f'{len(variants)} variants, {len(chosen)} documents chosen, {len(pending)} to read', flush=True)

    ready = Queue(maxsize=args.workers * 4)
    threading.Thread(target=downloader, args=(pending, urls, ready, args.pause), daemon=True).start()
    failed, started = 0, time.time()
    with ProcessPoolExecutor(args.workers) as pool, RESULTS.open('a') as log:
        inflight = {}

        def finish(efta, future):
            texts = future.result()
            ids = {name: addresses(ocr_text(texts[name]) if name != 'text_layer' else texts[name], key) for name in ENGINES}
            for member in sorted(chosen[efta]):
                canonical = variants[member]
                family = same_identity.get(canonical, set()) | {canonical}
                log.write(json.dumps({'efta_number': efta, 'variant_id': member, 'canonical_id': canonical, 'corpus_hit_pages': pages[efta], 'pages_read': texts['pages_read'],
                                      'verdicts': {name: verdict(member, family, ids[name]) for name in ENGINES}}) + '\n')
            log.flush()

        count = 0
        while (item := ready.get()) is not None:
            if isinstance(item, Exception):
                raise RuntimeError('downloader failed') from item
            efta, ok = item
            if not ok:
                failed += 1
                continue
            inflight[efta] = pool.submit(read_pdf, (str(WORK / f'{efta}.pdf'), pages[efta], args.dpi, args.max_pages))
            for e in [e for e, f in inflight.items() if f.done()]:
                finish(e, inflight.pop(e))
                count += 1
                if count % 50 == 0:
                    rate = count / (time.time() - started)
                    print(f'{count}/{len(pending)} read, {failed} download failures, ~{(len(pending) - count) / rate / 3600:.1f} h left', flush=True)
            while len(inflight) >= args.workers * 2:
                e = next(iter(inflight))
                finish(e, inflight.pop(e))
                count += 1
        for e, f in inflight.items():
            finish(e, f)

    # Last record per (document, variant): an interrupted run may log a document twice.
    latest = {(r['efta_number'], r['variant_id']): r for r in map(json.loads, RESULTS.read_text().splitlines())}
    results = [r for r in latest.values() if r['efta_number'] in chosen]
    summary = summarize(results, len(variants), len(chosen))
    summary['download_failures_this_run'] = failed
    (ROOT / 'output/reocr_full.json').write_text(json.dumps({'summary': summary, 'results': results}, indent=1) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
