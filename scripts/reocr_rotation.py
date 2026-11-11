"""Local-only PILOT: detect turned scans and measure text recovered by re-OCR.

    .venv/bin/python -B scripts/reocr_rotation.py [--limit 50] [--workers 3] [--dpi 300]
    .venv/bin/python -B scripts/reocr_rotation.py --calibration-pdf /local/known.pdf

Only existing data/external/reocr/EFTA*.pdf files are scanned; no network calls.
At 75 dpi, detrended text-line projections gate four-angle Tesseract --psm 6
at --dpi. Angles mean PIL.rotate(angle, expand=True), counterclockwise. Page
numbers in all artifacts are ZERO-BASED. Projection cannot detect 180-degree
turns in otherwise horizontal pages: those are tested only in the gated subset.

Resumable page records: data/external/reocr/rotation_results.jsonl (IDs/counts).
Recovered text: data/private/reocr_rotation/<EFTA>_<page>.txt, mode 0600.
Summary: output/reocr_rotation.json (public KG labels only, never raw emails).
Calibration uses OCR-labelled natural pages, the known table on page index 14,
and controlled turns of OCR-confirmed horizontal pages. These groups are
reported separately; controlled turns do not estimate natural prevalence.
--limit chooses a reproducible spread of documents, not the first sorted batch.
Completed pages are skipped; errors are logged separately and retried on rerun.
Keep DPI/detector settings unchanged when resuming (mismatches are rejected).
"""
import argparse
import json
import multiprocessing
import os
import re
import subprocess
import tempfile
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

import numpy as np
from PIL import Image

from build_contacts import contact_id, contact_key
import corpus as corpus_source

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'data/external/reocr'
RESULTS = WORK / 'rotation_results.jsonl'
PRIVATE = ROOT / 'data/private/reocr_rotation'
SUMMARY = ROOT / 'output/reocr_rotation.json'
CALIBRATION = WORK / 'rotation_calibration.json'
VERSION = 1
LOW_DPI = 75
ANGLES = (0, 90, 270, 180)
TOKEN = re.compile(r"[A-Za-z]{3,}(?:'[A-Za-z]+)?")
EMAIL = re.compile(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+')
WORDS = None
ENTITIES = None
KEY = None
DB = None


def dictionary():
    return {w.strip().casefold() for w in Path('/usr/share/dict/words').read_text().splitlines()}


def word_score(text, words):
    tokens = TOKEN.findall(text or '')
    good = sum(t.casefold() in words for t in tokens)
    ratio = good / len(tokens) if tokens else 0.0
    return {'words': len(tokens), 'dictionary_words': good, 'ratio': round(ratio, 6),
            'score': round(good * ratio, 6)}


def orientation(image):
    """High-frequency row/column ink energy, ignoring borders and solid rules.

    Horizontal text produces repeated dark rows; sideways text dark columns.
    Ambiguous pages receive OCR too. Blank/near-blank pages do not.
    """
    a = np.asarray(image.convert('L'))
    h, w = a.shape
    a = a[max(1, h // 30):h - max(1, h // 30), max(1, w // 30):w - max(1, w // 30)] < 180
    if not a.size:
        return {'class': 'blank', 'row_column_ratio': 1.0, 'ink_fraction': 0.0}
    # Table borders and redaction bars otherwise dominate projection variance.
    a[a.mean(axis=1) > .45, :] = False
    a[:, a.mean(axis=0) > .45] = False
    density = float(a.mean())
    if density < .001 or int(a.sum()) < 60:
        return {'class': 'blank', 'row_column_ratio': 1.0, 'ink_fraction': round(density, 6)}

    def energy(values):
        k = min(15, len(values) // 2 * 2 - 1)
        smooth = np.convolve(np.pad(values, k // 2, mode='edge'), np.ones(k) / k, mode='valid')
        return float(np.mean((values - smooth) ** 2))

    ratio = (energy(a.mean(axis=1)) + 1e-8) / (energy(a.mean(axis=0)) + 1e-8)
    label = 'sideways' if ratio < .65 else 'upright' if ratio > 1.8 else 'uncertain'
    return {'class': label, 'row_column_ratio': round(ratio, 5), 'ink_fraction': round(density, 6)}


def clearly_better(best, upright):
    """Require sufficient readable words and both absolute and relative gain."""
    return (best['dictionary_words'] >= 12 and best['ratio'] >= .45
            and best['score'] >= upright['score'] * 1.35 + 5)


def sideways_label(scores):
    """OCR label for line direction, independent of the image gate.

    Compare axis groups: uncertainty between 90 and 270 does not make the
    sideways label uncertain. Likewise, 0 versus 180 both mean horizontal.
    """
    scores = {int(a): s for a, s in scores.items()}
    horizontal = max((scores[a] for a in (0, 180)), key=lambda s: s['score'])
    sideways = max((scores[a] for a in (90, 270)), key=lambda s: s['score'])
    if clearly_better(sideways, horizontal):
        return True
    if clearly_better(horizontal, sideways):
        return False
    return None


def normalized(text):
    return ' '.join(re.findall(r'[a-z0-9]+', (text or '').casefold()))


def entity_index():
    index, labels = {}, {}
    for filename, kind in (('graph_entities.json', 'persons'), ('company_nodes.json', 'companies')):
        for row in json.loads((ROOT / 'output' / filename).read_text()):
            if kind == 'persons' and row.get('entity_type') != 'person':
                continue
            identity = str(row['id'])
            labels[(kind, identity)] = row['name']
            spellings = [row['name']] + row.get('original_names', [])
            for spelling in spellings:
                phrase = normalized(spelling)
                # Avoid ambiguous single-word person matches and short labels.
                if len(phrase) < 5 or (kind == 'persons' and len(phrase.split()) < 2):
                    continue
                index.setdefault(phrase.split()[0], []).append((phrase, kind, identity))
    return index, labels


def mentions(text, index):
    plain = ' ' + normalized(text) + ' '
    found = {'persons': set(), 'companies': set()}
    for token in set(plain.split()):
        for phrase, kind, identity in index.get(token, ()):
            if ' ' + phrase + ' ' in plain:
                found[kind].add(identity)
    return found


def recovered_entities(new, old, index, key):
    a, b = mentions(new, index), mentions(old, index)
    out = {kind: sorted(a[kind] - b[kind]) for kind in a}
    emails = lambda t: {m.casefold().strip('.,;:') for m in EMAIL.findall(t or '')}
    out['emails'] = sorted(contact_id(e, 'email', key) for e in emails(new) - emails(old))
    return out


def initialize():
    global WORDS, ENTITIES, KEY, DB
    WORDS = dictionary()
    ENTITIES = entity_index()[0]
    KEY = contact_key()
    DB = corpus_source.connect()


def render(pdf, page, dpi, tmp):
    prefix = str(Path(tmp) / 'page')
    subprocess.run(['pdftoppm', '-f', str(page + 1), '-l', str(page + 1), '-r', str(dpi),
                    '-gray', '-singlefile', pdf, prefix], check=True, capture_output=True, timeout=90)
    with Image.open(prefix + '.pgm') as im:
        return im.copy()


def ocr_angles(image, tmp):
    scores, texts = {}, {}
    for angle in ANGLES:
        path = Path(tmp) / 'ocr.png'
        turned = image.rotate(angle, expand=True) if angle else image
        turned.save(path)
        done = subprocess.run(['tesseract', str(path), 'stdout', '-l', 'eng', '--psm', '6'],
                              capture_output=True, text=True, check=True, timeout=90,
                              env={**os.environ, 'OMP_THREAD_LIMIT': '1'})
        texts[angle] = done.stdout
        scores[angle] = word_score(done.stdout, WORDS)
    best = max(ANGLES, key=lambda angle: scores[angle]['score'])
    return best, scores, texts


def pdf_pages(pdf):
    result = subprocess.run(['pdfinfo', str(pdf)], check=True, capture_output=True, text=True, timeout=30)
    return int(re.search(r'^Pages:\s+(\d+)', result.stdout, re.M).group(1))


def scan_page(job):
    pdf, page, dpi = job
    efta = Path(pdf).stem
    record = {'version': VERSION, 'dpi': dpi, 'efta_number': efta, 'page': page}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            detector = orientation(render(pdf, page, LOW_DPI, tmp))
            record.update(detector=detector, angle=0, ocr_checked=False)
            if detector['class'] in ('sideways', 'uncertain'):
                image = render(pdf, page, dpi, tmp)
                best, scores, texts = ocr_angles(image, tmp)
                rotated = best != 0 and clearly_better(scores[best], scores[0])
                record.update(ocr_checked=True, scores=scores, best_angle=best, angle=best if rotated else 0)
                if rotated:
                    row = DB.execute('SELECT text FROM pages WHERE doc_id=? AND page=?', (efta, page)).fetchone()
                    old = row[0] or '' if row else ''
                    record['doj_available'] = row is not None
                    record['doj'] = word_score(old, WORDS)
                    record['turned'] = scores[best]
                    record['recovered'] = recovered_entities(texts[best], old, ENTITIES, KEY) if row else {
                        'persons': [], 'companies': [], 'emails': []}
                    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
                    destination = PRIVATE / f'{efta}_{page}.txt'
                    # Atomic text replacement; permissions applied before any text is written.
                    fd, temporary = tempfile.mkstemp(dir=PRIVATE)
                    with os.fdopen(fd, 'w') as stream:
                        stream.write(texts[best])
                    os.replace(temporary, destination)
        return record
    except Exception as error:
        # Never log subprocess stderr, which may contain extracted text.
        return {**record, 'error': type(error).__name__}


def calibrate_page(job):
    pdf, page, turn, group = job
    try:
        with tempfile.TemporaryDirectory() as tmp:
            image = render(pdf, page, LOW_DPI, tmp)
            if turn:
                image = image.rotate(turn, expand=True)
            detected = orientation(image)
            high = render(pdf, page, 150, tmp)
            if turn:
                high = high.rotate(turn, expand=True)
            best, scores, _ = ocr_angles(high, tmp)
        # Supplied case is independently labelled; other labels require OCR separation.
        label = True if group == 'known' else sideways_label(scores)
        return {'efta_number': Path(pdf).stem, 'page': page, 'controlled_turn': turn, 'group': group,
                'detector': detected, 'label_sideways': label, 'best_angle': best, 'scores': scores}
    except Exception as error:
        return {'efta_number': Path(pdf).stem, 'page': page, 'group': group, 'error': type(error).__name__}


def calibration_metrics(rows):
    def metrics(items):
        labelled = [r for r in items if r.get('label_sideways') is not None]
        tp = sum(r['label_sideways'] and r['detector']['class'] != 'upright' and r['detector']['class'] != 'blank' for r in labelled)
        fp = sum(not r['label_sideways'] and r['detector']['class'] in ('sideways', 'uncertain') for r in labelled)
        fn = sum(r['label_sideways'] and r['detector']['class'] not in ('sideways', 'uncertain') for r in labelled)
        tn = len(labelled) - tp - fp - fn
        return {'labelled': len(labelled), 'unlabelled': len(items) - len(labelled),
                'true_positive': tp, 'false_positive': fp, 'false_negative': fn, 'true_negative': tn,
                'precision': round(tp / (tp + fp), 4) if tp + fp else None,
                'recall': round(tp / (tp + fn), 4) if tp + fn else None}
    return {'gate_sideways_or_uncertain': metrics(rows),
            'natural_and_known': metrics([r for r in rows if r['group'] != 'controlled']),
            'controlled': metrics([r for r in rows if r['group'] == 'controlled']),
            'note': 'Small purposive OCR-labelled set; controlled turns are artificial positives. '
                    'Unknown OCR labels excluded. Measures sideways/uncertain gate, not 180-degree detection.'}


def calibrate(pool, pdfs, known, dpi):
    if CALIBRATION.exists():
        saved = json.loads(CALIBRATION.read_text())
        if (saved.get('version') == VERSION and saved.get('validation_dpi') == dpi
                and (saved.get('known_case_available') or not known.exists())):
            return saved
    # First pages spread across the cached cohort, selected before OCR labelling.
    sample = [pdfs[i] for i in np.linspace(0, len(pdfs) - 1, min(12, len(pdfs)), dtype=int)]
    jobs = [(str(p), 0, 0, 'natural') for p in sample]
    if known.exists():
        jobs.append((str(known), 14, 0, 'known'))
    rows = list(pool.map(calibrate_page, jobs))
    horizontal = [r for r in rows if r.get('label_sideways') is False and r['best_angle'] == 0][:4]
    by_name = {p.stem: p for p in sample}
    controlled = [(str(by_name[r['efta_number']]), r['page'], angle, 'controlled')
                  for r in horizontal for angle in (90, 270)]
    rows.extend(pool.map(calibrate_page, controlled))
    validation = pool.submit(scan_page, (str(known), 14, dpi)).result() if known.exists() else None
    result = {'version': VERSION, 'ocr_dpi': 150, 'detector_dpi': LOW_DPI,
              'validation_dpi': dpi, 'known_case_validation': validation,
              'known_case_available': known.exists(), 'pages': rows,
              'metrics': calibration_metrics(rows)}
    CALIBRATION.write_text(json.dumps(result, indent=2) + '\n')
    print('Calibration: ' + json.dumps(result['metrics']), flush=True)
    return result


def audit_gate(calibration, records):
    """Add up to eight real gated pages, so precision includes difficult negatives.

    OCR labelling is independent of projection; inconclusive OCR is unlabelled.
    Thresholds are fixed before these labels, never fitted to the audit.
    """
    rows = [r for r in calibration['pages'] if r['group'] != 'natural_gate_audit']
    candidates = sorted((r for r in records.values() if r['ocr_checked']),
                        key=lambda r: (r['efta_number'], r['page']))
    gated = [r for r in candidates if r['detector']['class'] == 'sideways'][:4]
    gated += [r for r in candidates if r['detector']['class'] == 'uncertain'][:8 - len(gated)]
    for r in gated:
        scores = {int(a): s for a, s in r['scores'].items()}
        order = sorted(scores, key=lambda a: scores[a]['score'], reverse=True)
        rows.append({'efta_number': r['efta_number'], 'page': r['page'], 'group': 'natural_gate_audit',
                     'detector': r['detector'], 'label_sideways': sideways_label(scores),
                     'best_angle': order[0], 'scores': r['scores']})
    calibration['pages'] = rows
    calibration.update(ocr_dpi=150, detector_dpi=LOW_DPI)
    calibration['metrics'] = calibration_metrics(rows)
    CALIBRATION.write_text(json.dumps(calibration, indent=2) + '\n')


def read_results():
    records = {}
    if RESULTS.exists():
        # Repair only a truncated final line after an interrupted append.
        with RESULTS.open('rb+') as stream:
            while True:
                position = stream.tell()
                line = stream.readline()
                if not line:
                    break
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    if stream.read(1):
                        raise ValueError('Invalid non-final results line')
                    stream.truncate(position)
                    break
                records[(r['efta_number'], r['page'])] = r
    return records


def summarize(records, selected, calibration, run, cohort_pages):
    rows = [r for (efta, _), r in records.items() if efta in selected]
    rotated = [r for r in rows if r['angle']]
    compared = [r for r in rotated if r['doj_available']]
    labels = entity_index()[1]
    recovered = {kind: set(i for r in compared for i in r['recovered'][kind]) for kind in ('persons', 'companies', 'emails')}
    examples = sorted(compared, key=lambda r: (-sum(len(v) for v in r['recovered'].values()), -r['turned']['score'], r['efta_number'], r['page']))[:20]
    mean = lambda name: round(sum(r[name]['ratio'] for r in compared) / len(compared), 4) if compared else None
    return {'pilot': True, 'page_numbering': 'zero-based', 'version': VERSION,
            'documents_selected': len(selected), 'documents_scanned': len({r['efta_number'] for r in rows}),
            'cached_documents': len(list(WORK.glob('EFTA*.pdf'))), 'cohort_pages_corpus': cohort_pages,
            'pages_scanned': len(rows), 'pages_ocr_checked': sum(r['ocr_checked'] for r in rows),
            'settings': {'ocr_dpi': next((r['dpi'] for r in rows), None), 'detector_dpi': LOW_DPI,
                         'sideways_below': .65, 'upright_above': 1.8, 'ocr_psm': 6},
            'detector_classes': dict(Counter(r['detector']['class'] for r in rows)),
            'pages_rotated': len(rotated), 'pages_rotated_by_angle': dict(Counter(str(r['angle']) for r in rotated)),
            'detector_calibration': calibration['metrics'], 'known_case_available': calibration['known_case_available'],
            'known_case_validation': calibration.get('known_case_validation'),
            'mean_ratio_doj': mean('doj'), 'mean_ratio_turned_ocr': mean('turned'),
            'comparison_pages': len(compared), 'missing_doj_pages': len(rotated) - len(compared),
            'word_counts': {name: sum(r[name]['words'] for r in compared) for name in ('doj', 'turned')},
            'recovered_unique': {kind: len(ids) for kind, ids in recovered.items()},
            'recovered_page_mentions': {kind: sum(len(r['recovered'][kind]) for r in compared) for kind in recovered},
            'top_examples': [{'efta_number': r['efta_number'], 'page': r['page'], 'angle': r['angle'],
                              'persons': [{'id': i, 'name': labels.get(('persons', i))} for i in r['recovered']['persons']],
                              'companies': [{'id': i, 'name': labels.get(('companies', i))} for i in r['recovered']['companies']],
                              'email_ids': r['recovered']['emails']} for r in examples],
            'run': run, 'limitations': ['Horizontal pages are not tested for 180-degree rotation.',
                'Projection may miss mixed-orientation text, sparse pages, handwriting and images.',
                'Recovered entities are bounded normalized KG-name matches, not verified identities.',
                'Dictionary ratios use alphabetic tokens of three or more letters; no semantic accuracy claim.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--limit', type=int, help='reproducible spread of N cached documents')
    parser.add_argument('--dpi', type=int, default=300)
    parser.add_argument('--calibration-pdf', type=Path, default=WORK / 'EFTA00075562.pdf', help='local PDF of known table, EFTA00075562')
    args = parser.parse_args()
    if args.workers < 1 or args.dpi < 75 or (args.limit is not None and args.limit < 1):
        parser.error('workers/limit must be positive; dpi must be at least 75')
    os.umask(0o077)
    WORK.mkdir(parents=True, exist_ok=True)
    contact_key()  # Validate namespace before starting any workers.
    pdfs = sorted(WORK.glob('EFTA*.pdf'))
    if not pdfs:
        parser.error('No cached PDFs')
    selected_pdfs = [pdfs[i] for i in np.linspace(0, len(pdfs) - 1, min(args.limit or len(pdfs), len(pdfs)), dtype=int)]
    selected = {p.stem for p in selected_pdfs}
    records = read_results()
    if any(r.get('version') != VERSION or r.get('dpi') != args.dpi for r in records.values()):
        parser.error('Existing results use different settings; archive rotation artifacts before changing settings')
    with corpus_source.connect() as db:
        cohort_pages = sum((db.execute('SELECT total_pages FROM main.documents WHERE efta_number=?', (p.stem,)).fetchone() or [0])[0] or 0 for p in pdfs)
    started = time.monotonic()
    new_pages, errors = 0, 0
    calibration_seconds = 0
    # Python 3.14's default forkserver needs an AF_UNIX listener, unavailable in
    # some local sandboxes. No threads are running when this pool forks.
    with ProcessPoolExecutor(args.workers, initializer=initialize,
                             mp_context=multiprocessing.get_context('fork')) as pool:
        calibration = calibrate(pool, pdfs, args.calibration_pdf, args.dpi)
        calibration_seconds = time.monotonic() - started
        scan_started = time.monotonic()
        def jobs():
            nonlocal errors
            for pdf in selected_pdfs:
                try:
                    count = pdf_pages(pdf)
                except Exception as error:
                    errors += 1
                    error_log.write(json.dumps({'efta_number': pdf.stem, 'error': type(error).__name__}) + '\n')
                    continue
                for page in range(count):
                    if (pdf.stem, page) not in records:
                        yield (str(pdf), page, args.dpi)
        with RESULTS.open('a') as log, (WORK / 'rotation_errors.jsonl').open('a') as error_log:
            pending, iterator = {}, iter(jobs())
            exhausted = False
            while pending or not exhausted:
                while len(pending) < args.workers * 2 and not exhausted:
                    job = next(iterator, None)
                    if job is None:
                        exhausted = True
                    else:
                        pending[pool.submit(scan_page, job)] = job
                if not pending:
                    break
                done, _ = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
                for future in done:
                    pending.pop(future)
                    r = future.result()
                    if 'error' in r:
                        error_log.write(json.dumps(r) + '\n')
                        error_log.flush()
                        errors += 1
                    else:
                        log.write(json.dumps(r) + '\n')
                        log.flush()
                        records[(r['efta_number'], r['page'])] = r
                        new_pages += 1
                if not done or new_pages % 25 < len(done):
                    elapsed = time.monotonic() - scan_started
                    print(f'{new_pages} new pages; {new_pages / max(elapsed, .001):.3f} pages/s; {errors} errors', flush=True)
    elapsed = time.monotonic() - scan_started
    rate = new_pages / elapsed if elapsed else 0
    run = {'new_pages': new_pages, 'scan_seconds': round(elapsed, 2), 'calibration_seconds': round(calibration_seconds, 2),
           'pages_per_second': round(rate, 4), 'errors': errors,
           'projected_full_hours': round(cohort_pages / rate / 3600, 3) if rate else None}
    audit_gate(calibration, records)
    report = summarize(records, selected, calibration, run, cohort_pages)
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    temp = SUMMARY.with_suffix('.json.tmp')
    temp.write_text(json.dumps(report, indent=2) + '\n')
    temp.replace(SUMMARY)
    print(json.dumps({k: report[k] for k in ('pages_scanned', 'pages_ocr_checked', 'pages_rotated', 'pages_rotated_by_angle',
                                            'mean_ratio_doj', 'mean_ratio_turned_ocr', 'recovered_unique', 'run')}), flush=True)
    print(f'Summary -> {SUMMARY.relative_to(ROOT)}', flush=True)


if __name__ == '__main__':
    main()
