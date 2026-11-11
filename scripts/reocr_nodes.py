"""Re-read suspicious viewer nodes on matched source lines, without rebuilding.

    .venv/bin/python -B scripts/reocr_nodes.py --pilot --limit 40 --cached-only

DOJ text, Tesseract and RapidOCR vote once each per node/page. Optional OnnxTR
and Luna are disagreement readers; Luna never supplies a second automatic vote.
Raw lines/crops/cache and scan-backed aliases stay in data/private/. Public
graph files and extraction signatures are never touched. Page numbers are zero
based, corpus page_ids are retained. No graph.json/graph_entities.json is read.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
import csv
from difflib import SequenceMatcher
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import random
import re
import resource
import sqlite3
import subprocess
import tempfile
import time

from rapidfuzz import fuzz, process
from rapidfuzz.distance import Levenshtein

from ocr_consensus import plausible_reading, OCR_ENGINES, spelling_key

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'data/external/reocr'
PRIVATE = ROOT / 'data/private/ocr_review'
CACHE = PRIVATE / 'node_pages'
OUT = PRIVATE / 'node_reocr.json'
CONSENSUS = ROOT / 'data/private/ocr_consensus.json'
VERSION = 2  # line-span/cache contract; consensus interchange stays version 1
BASE_ENGINES = ('doj', 'tesseract', 'rapidocr')


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=1)
            stream.write('\n')
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def readonly(path):
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    db.execute('PRAGMA cache_size=-4096')
    db.execute('PRAGMA temp_store=FILE')
    return db


def label(node):
    return node.get('real') or node['label']


def kind(node):
    identity = str(node['id'])
    if identity.startswith('email_') or node.get('sub') == 'email':
        return 'email'
    if identity.startswith('company_'):
        return 'company'
    return node['type']


def compact(value):
    return ''.join(c for c in value.casefold() if c.isalnum())


def suspicious(nodes):
    """Candidate selection only: no spelling similarity ever creates an alias."""
    from build_communications import ocr_artifacts, ocr_repair
    reasons, peers = defaultdict(set), defaultdict(set)
    choices, orders = defaultdict(dict), defaultdict(list)
    for node in nodes:
        name, identity = label(node), str(node['id'])
        if node['type'] in ('survivor', 'vehicle') or node.get('sub') == 'phone':
            continue
        words = re.findall(r'[^\W\d_]+', name)
        address = node.get('sub') == 'address' or bool(re.search(r'\b\d+\s+\w+.*\b(street|road|ave|avenue|lane|blvd|drive|rd|st)\b', name, re.I))
        if len(name.split()) > 5 and not address:
            reasons[identity].add('long_non_address')
        domain = bool(re.search(r'\.(com|org|net|edu|gov|corn|eom)\b', name, re.I))
        if domain and ('@' not in name or re.search(r'[()®<>\[\]]|\.corn\b|\.eom\b', name, re.I)):
            reasons[identity].add('garbled_contact')
        if '@' in name and (ocr_artifacts(name) or ocr_repair(name)[1] != name.rsplit('@', 1)[1].casefold()):
            reasons[identity].add('garbled_contact')
        if (re.search(r'[\d_®<>\[\]{}=|]', name) and not address and node['type'] != 'contact'
                or any(len(w) >= 19 for w in words)
                or any(re.search(r'[bcdfghjklmnpqrstvwxz]{6}', w, re.I) for w in words)
                or (words and len(words) >= 2 and (len(words[0]) <= 2 or len(words[-1]) <= 2))):
            reasons[identity].add('ocr_looking')
        group = 'contact' if domain or '@' in name or node['type'] in ('website', 'contact') else 'name'
        key = compact(name)
        if 5 <= len(key) <= 120:
            choices[group].setdefault(key, []).append(identity)
        if len(words) >= 2:
            orders[tuple(sorted(w.casefold() for w in words))].append((identity, spelling_key(name)))
    for group in orders.values():
        if len({s for _, s in group}) > 1:
            for identity, _ in group:
                reasons[identity].add('word_order_or_punctuation')
                peers[identity].update(i for i, _ in group if i != identity)
    for group in choices.values():
        keys = list(group)
        for key in keys:
            for other, distance, _ in process.extract(key, keys, scorer=Levenshtein.distance, score_cutoff=2, limit=12):
                if not distance or min(len(key), len(other)) < 6 or fuzz.ratio(key, other) < 78:
                    continue
                for identity in group[key]:
                    reasons[identity].add('near_ocr_edit')
                    peers[identity].update(group[other])
    return [{**node, 'reasons': sorted(reasons[str(node['id'])]), 'peers': sorted(peers[str(node['id'])])}
            for node in nodes if reasons[str(node['id'])]]


def choose_nodes(nodes, args):
    suspects = suspicious(nodes)
    if getattr(args, 'all_nodes', False):
        by_id = {str(n['id']): n for n in suspects}
        suspects = [{**n, 'reasons': by_id.get(str(n['id']), {}).get('reasons', ['requested']),
                     'peers': by_id.get(str(n['id']), {}).get('peers', [])} for n in nodes]
    if args.kinds:
        kinds = set(','.join(args.kinds).split(','))
        suspects = [n for n in suspects if kind(n) in kinds or n['type'] in kinds
                    or ('entity' in kinds and str(n['id']).startswith('entity_'))]
    if args.names:
        terms = [t.casefold() for t in args.names]
        # Explicit names may also request a clean control node.
        by_id = {str(n['id']): n for n in suspects}
        suspects = [{**n, 'reasons': by_id.get(str(n['id']), {}).get('reasons', ['requested']),
                     'peers': by_id.get(str(n['id']), {}).get('peers', [])}
                    for n in nodes if any(t in label(n).casefold() for t in terms)
                    and (not args.kinds or kind(n) in kinds or n['type'] in kinds)]
    rng = random.Random(args.seed)
    rng.shuffle(suspects)
    if args.pilot:
        families = ('cotrin', 'johana', 'martinez', 'jeevacation')
        preferred = [n for n in nodes if any(t in label(n).casefold() for t in families)]
        if args.kinds:
            preferred = [n for n in preferred if kind(n) in kinds or n['type'] in kinds]
        by_id = {str(n['id']): n for n in suspects}
        chosen = []
        for family in families:
            chosen += [{**n, 'reasons': by_id.get(str(n['id']), {}).get('reasons', ['pilot_family']),
                        'peers': by_id.get(str(n['id']), {}).get('peers', [])}
                       for n in preferred if family in label(n).casefold()][:5]
        used = {str(n['id']) for n in chosen}
        suspects = chosen + [n for n in suspects if str(n['id']) not in used]
    return suspects[:args.limit] if args.limit is not None else suspects


def matched_line(text, raw, threshold=72):
    """Locate the spelling, keeping context for projecting its engine span."""
    lines = [s.strip() for s in (text or '').splitlines() if s.strip()]
    if not lines:
        return None
    probe = spelling_key(raw)
    candidates = [(line, i) for i, line in enumerate(lines)]
    # Two physical lines can contain one wrapped name/mailbox.
    candidates += [(' '.join(lines[i:i + 2]), i) for i in range(len(lines) - 1)]
    def score(item):
        line, i = item
        a = fuzz.partial_ratio(probe, spelling_key(line))
        a *= min(1, len(compact(line)) / max(1, len(compact(raw))))
        return a, -len(line), -i
    best = max(candidates, key=score)
    confidence = score(best)[0]
    return {'line': best[0], 'index': best[1], 'match': round(confidence / 100, 4)} if confidence >= threshold else None


def raw_span(raw, line):
    """A mention anchor, allowing <=2 OCR edits but not a neighbouring phrase."""
    probe = compact(raw)
    positions = [i for i, c in enumerate(line) for folded in c.casefold() if folded.isalnum()]
    source = compact(line)
    if not probe or not source:
        return None
    start = source.find(probe)
    if start >= 0:
        end = start + len(probe)
    else:
        hit = fuzz.partial_ratio_alignment(probe, source)
        if hit is None:
            return None
        start, end = hit.dest_start, hit.dest_end
        candidate = source[start:end]
        if (Levenshtein.distance(probe, candidate) > (2 if len(probe) >= 6 else 1)
                or min(len(probe), len(candidate)) / max(len(probe), len(candidate)) < .8):
            return None
    return positions[start], positions[end - 1] + 1


def reading_span(raw, source_line, engine_line):
    """Project the mention through a line alignment; never choose a known name.

    Engine output must match the surrounding corpus line too. Expanding the
    projected end to the word boundary recovers inserted letters in a surname.
    """
    if not engine_line or fuzz.ratio(spelling_key(source_line), spelling_key(engine_line)) < 48:
        return None
    source, target = ' '.join(source_line.split()), ' '.join(engine_line.split())
    bounds = raw_span(raw, source)
    if bounds is None:
        return None
    start, end = bounds
    # Case folding can expand one Unicode character (ß -> ss). Match in folded
    # coordinates, then map back before slicing the original engine spelling.
    start = len(source[:start].casefold())
    end = len(source[:end].casefold())
    positions = []
    for tag, a, b, c, d in SequenceMatcher(None, source.casefold(), target.casefold(), autojunk=False).get_opcodes():
        if tag == 'insert':
            if start < a < end:
                positions.extend((c, d))
        elif a < end and b > start:
            if tag == 'equal':
                positions.extend((c + max(start, a) - a, c + min(end, b) - a))
            else:
                positions.extend((c, d))
    if not positions:
        return None
    left, right = min(positions), max(positions)
    target_positions = [i for i, c in enumerate(target) for _ in c.casefold()]
    left = target_positions[left] if left < len(target_positions) else len(target)
    right = target_positions[right - 1] + 1 if right else left
    # Only complete a word, not an adjacent header field or a neighbouring name.
    # Do not expand left: RapidOCR often glues preceding "to"/"mailto"
    # directly onto a mailbox. The alignment already locates its start.
    while right < len(target) and right > 0 and target[right - 1].isalnum() and target[right].isalnum():
        right += 1
    result = target[left:right].strip(' \t<>"\';,:')
    if '@' in result:
        result = re.sub(r'\s*([@.])\s*', r'\1', result)
    return result if result and len(result) <= max(240, len(raw) * 3) else None


def vote(raw, readings, corpus_spelling=None):
    groups = defaultdict(list)
    for engine, reading in readings.items():
        value = reading.get('spelling')
        # A projected span that is not a reading of this spelling (neighbouring
        # field, truncated name, other number) casts no vote.
        if engine in OCR_ENGINES and value and plausible_reading(raw, value):
            groups[spelling_key(value)].append(engine)
    ordered = sorted(groups.items(), key=lambda p: (-len(p[1]), p[0]))
    if not ordered or len(ordered[0][1]) < 2 or (len(ordered) > 1 and len(ordered[0][1]) == len(ordered[1][1])):
        return {'verdict': 'disagree', 'agreement': 0, 'engines': [], 'consensus': None}
    key, engines = ordered[0]
    value = readings[next((e for e in ('rapidocr', 'tesseract', 'onnxtr', 'doj') if e in engines))]['spelling']
    return {'verdict': 'same' if key == spelling_key(corpus_spelling or raw) else 'corrected', 'consensus': value,
            'engines': sorted(engines), 'agreement': round(len(engines) / max(3, len(set(readings) & OCR_ENGINES)), 4)}


def consensus_map(report):
    """Keep unanimous *page outcomes*, not unions of engines across pages.

    A confident unchanged page blocks a global correction, as does a second
    confident spelling. Disagreements/missing pages do not supply evidence.
    """
    by_raw = defaultdict(list)
    for node in report:
        for check in node.get('checks', []):
            if check.get('consensus') and len(set(check.get('engines', [])) & OCR_ENGINES) >= 2:
                by_raw[check['raw']].append(check)
    variants, conflicts = {}, 0
    for raw, checks in by_raw.items():
        if not any(c['verdict'] == 'corrected' for c in checks):
            continue  # a display punctuation variant is not a corpus OCR repair
        targets = {spelling_key(c['consensus']) for c in checks}
        if len(targets) != 1:
            conflicts += 1
            continue
        if next(iter(targets)) == spelling_key(raw):
            continue
        # Deduplicate shared pages (several nodes can cite one mention).
        pages = {(c['doc_id'], c['page_id']): {k: c[k] for k in ('doc_id', 'page_id', 'page', 'engines', 'agreement')}
                 for c in checks}
        variants[raw] = {'consensus': checks[0]['consensus'], 'engines': sorted({e for c in checks for e in c['engines']}),
                         'pages': list(pages.values())}
    return {'version': 1, '_doc': 'Private exact spelling aliases; >=2 independent engines on each matched source line. '
            'Conflicting confident page readings are withheld. Never change raw node IDs or delete nodes.',
            'variants': variants, 'conflicting_spellings_withheld': conflicts}


def source_pages(nodes, root=ROOT, per_node=3, cached_only=False):
    """Indexed mention keys, or raw communication records -> corpus page ids.

    Raw communication IDs are queried first. Canonical member IDs are fallbacks
    only; every chosen page must still contain this node's spelling.
    """
    import corpus
    from build_companies import normal_name
    cached = {p.stem for p in (root / 'data/external/reocr').glob('*.pdf')}
    dbs = {}
    for name, relative in [('comm', 'output/communication_graph.db'), ('company', 'output/.build/company_mentions.db'),
                           ('entity', 'output/.build/entity_mentions.db'), ('resolved', 'output/entity_graph.db'),
                           ('comention', 'output/comention_graph.db')]:
        preferred = {'entity':'data/private/entity_mentions.db', 'resolved':'data/private/entity_layer/entity_graph.db'}.get(name)
        if preferred and (root/preferred).exists(): relative=preferred
        if (root / relative).exists():
            dbs[name] = readonly(root / relative)
    corpus_db = corpus.connect(root / 'data/raw/full_text_corpus.db')
    corpus_db.execute('PRAGMA cache_size=-4096')
    results = {}
    wanted = {str(n['id']) for n in nodes}
    graph_ids = {'kg:' + i if i.isdigit() else i for i in wanted}
    keys_by_node, members_by_node = defaultdict(list), defaultdict(list)
    # These reverse columns are not indexed in existing build databases. Batch
    # both scans once, keeping only bounded keys/members for requested nodes.
    if 'resolved' in dbs:
        for key, identity in dbs['resolved'].execute('SELECT key,entity_id FROM key_nodes'):
            identity = str(identity)
            if identity in wanted and len(keys_by_node[identity]) < 60:
                keys_by_node[identity].append(key)
    if 'comm' in dbs:
        for member, canonical in dbs['comm'].execute('SELECT member_id,canonical_id FROM identity_map'):
            if canonical in graph_ids and len(members_by_node[canonical]) < 150:
                members_by_node[canonical].append(member)
    located = {}
    def parent_document(doc):
        # EFTA citations are sometimes inner-page Bates IDs. A bounded indexed
        # predecessor lookup avoids corpus.page_locator's million-row arrays.
        if doc not in located:
            row = corpus_db.execute('SELECT id,meta FROM documents WHERE id<=? ORDER BY id DESC LIMIT 1', (doc,)).fetchone()
            parent = doc
            if row and re.fullmatch(r'EFTA\d{8}', doc or '') and re.fullmatch(r'EFTA\d{8}', row[0] or ''):
                count = json.loads(row[1] or '{}').get('total_pages') or 1
                if 0 <= int(doc[4:]) - int(row[0][4:]) < count:
                    parent = row[0]
            located[doc] = parent
        return located[doc]
    try:
        for node in nodes:
            identity, raw = str(node['id']), label(node)
            graph_id = 'kg:' + identity if identity.isdigit() else identity
            found = []
            # Entity graph records retain the extraction key even when the ID
            # was folded/retyped. Names must be recovered from private DB above
            # the extraction layer, not by rescanning its 30 GB source.
            if 'resolved' in dbs and 'entity' in dbs:
                keys = list(keys_by_node[identity])
                if identity.startswith('participant_'):
                    keys.append(identity)
                for key in set(keys):
                    found += [(r[0], r[1], 'entity_mentions') for r in dbs['entity'].execute(
                        'SELECT page_id,doc_id FROM mentions WHERE key=? LIMIT 300', (key,))]
            if 'company' in dbs and (kind(node) == 'company' or node['type'] == 'organization'):
                for page_id, doc in dbs['company'].execute(
                        'SELECT page_id,efta FROM mentions WHERE key=? LIMIT 300', (normal_name(raw),)):
                    found.append((page_id, doc, 'company_mentions'))
            docs = set()
            if 'comm' in dbs and not identity.startswith('entity_'):
                ids = [graph_id]
                # The reverse map was scanned once above. Evidence queries
                # always use indexed raw source/target IDs.
                ids += members_by_node[graph_id]
                for member in dict.fromkeys(ids):
                    for column in ('source_id', 'target_id'):
                        docs.update(r[0] for r in dbs['comm'].execute(
                            f'SELECT efta_number FROM evidence WHERE {column}=? AND efta_number IS NOT NULL LIMIT 600', (member,)))
                    if len(docs & cached) >= per_node:
                        break
            if identity.isdigit() and 'comention' in dbs:
                docs.update(r[0] for r in dbs['comention'].execute('SELECT efta_number FROM mentions WHERE entity_id=? LIMIT 300', (int(identity),)))
            docs = {parent_document(doc) for doc in docs if doc}
            # Direct mention pages are preferred. Communication records do not
            # have page IDs: locate their exact/fuzzy raw line inside that doc.
            planned = []
            for page_id, doc, source in sorted(set(found), key=lambda r: (r[1] not in cached, r[1] or '', r[0])):
                if cached_only and doc not in cached:
                    continue
                row = corpus_db.execute('SELECT doc_id,page,text FROM pages WHERE page_id=?', (page_id,)).fetchone()
                if row and row[0] == doc and (hit := matched_line(row[2], raw)) and raw_span(raw, hit['line']):
                    planned.append({'page_id': page_id, 'doc_id': doc, 'page': row[1], 'raw': raw, 'source': source,
                                    'corpus_line': hit['line'], 'corpus_match': hit['match']})
                if len({p['doc_id'] for p in planned}) >= per_node:
                    break
            for doc in sorted(docs, key=lambda d: (d not in cached, d)):
                if len({p['doc_id'] for p in planned}) >= per_node:
                    break
                if (cached_only and doc not in cached) or any(p['doc_id'] == doc for p in planned):
                    continue
                matches = []
                for page_id, page, text in corpus_db.execute('SELECT page_id,page,text FROM pages WHERE doc_id=? ORDER BY page LIMIT 80', (doc,)):
                    if (hit := matched_line(text, raw)) and raw_span(raw, hit['line']):
                        matches.append({'page_id': page_id, 'doc_id': doc, 'page': page, 'raw': raw,
                                        'source': 'communication_records' if not identity.isdigit() else 'communication_or_comention',
                                        'corpus_line': hit['line'], 'corpus_match': hit['match']})
                if matches:
                    planned.append(max(matches, key=lambda p: (p['corpus_match'], -p['page'])))
            unique = {}
            for plan in planned:
                unique.setdefault(plan['doc_id'], plan)
            results[identity] = list(unique.values())[:per_node]
    finally:
        corpus_db.close()
        for db in dbs.values():
            db.close()
    return results


class Downloader:
    """Sequential, rate limited, atomic, shared PDF cache; no visible browser."""
    def __init__(self, pause=0.5):
        self.pause, self.last, self.cookie = pause, 0, None

    def get(self, doc, url, cached_only=False):
        if not re.fullmatch(r'EFTA\d{8}', doc):
            return None
        path = WORK / f'{doc}.pdf'
        if path.exists():
            with path.open('rb') as stream:
                if stream.read(4) == b'%PDF':
                    return path
        if cached_only or not url or not url.startswith('https://www.justice.gov/'):
            return None
        from reocr_pilot import age_verified_cookies
        from reocr_full import UA
        WORK.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            time.sleep(max(0, self.pause - (time.monotonic() - self.last)))
            fd, tmp = tempfile.mkstemp(dir=WORK, suffix='.part')
            os.close(fd)
            try:
                run = subprocess.run(['curl', '-sL', '--fail', '--max-time', '45', '-A', UA,
                                      '-H', f'Cookie: {self.cookie or ""}', '-o', tmp, url], capture_output=True, timeout=50)
                with open(tmp, 'rb') as stream:
                    valid = run.returncode == 0 and stream.read(4) == b'%PDF'
                if valid:
                    os.replace(tmp, path)
                    return path
                if run.returncode:
                    return None  # offline/HTTP failure: do not launch a browser
                self.cookie = age_verified_cookies(url)
            finally:
                Path(tmp).unlink(missing_ok=True)
                self.last = time.monotonic()
        return None


def tesseract(path, tsv=False):
    return subprocess.run(['tesseract', str(path), 'stdout', '--psm', '6', *(['tsv'] if tsv else [])],
                          check=True, capture_output=True, text=True, timeout=90,
                          env={**os.environ, 'OMP_THREAD_LIMIT': '1'}).stdout


def tsv_rows(text):
    grouped = defaultdict(list)
    for row in csv.DictReader(io.StringIO(text), delimiter='\t'):
        if row.get('level') == '5' and (row.get('text') or '').strip():
            grouped[tuple(row[k] for k in ('page_num', 'block_num', 'par_num', 'line_num'))].append(row)
    rows = []
    for words in grouped.values():
        boxes = [(int(w['left']), int(w['top']), int(w['width']), int(w['height'])) for w in words]
        rows.append({'line': ' '.join(w['text'] for w in words), 'box': [min(b[0] for b in boxes), min(b[1] for b in boxes),
                     max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)]})
    return rows


def crop_box(rows, corpus_line, width, height):
    """Match the whole corpus line before asking the other OCR engines."""
    choices = [(i, row) for i, row in enumerate(rows)]
    if not choices:
        return None
    def score(pair):
        return fuzz.ratio(spelling_key(corpus_line), spelling_key(pair[1]['line']))
    i, row = max(choices, key=score)
    if score((i, row)) < 48:
        return None
    x0, y0, x1, y1 = row['box']
    pad = max(5, (y1 - y0) // 3)
    return [0, max(0, y0 - pad), width, min(height, y1 + pad)]


def initialize_worker():
    # Fresh spawned workers avoid inheriting corpus/URL structures. OCR is
    # single threaded. Hard RSS policing is
    # done by the parent, including Tesseract/Poppler child processes.
    os.environ.update(OMP_THREAD_LIMIT='1', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')


_rapid = None
_onnxtr = None


def rapid_lines(path):
    global _rapid
    if _rapid is None:
        import onnxruntime
        onnxruntime.set_default_logger_severity(3)
        from reocr_full import rapid_engine
        _rapid = rapid_engine(threads=1)
        # Existing factory caps detector size. Crops are much smaller still.
    from reocr_participants import rapid_rows
    return rapid_rows(_rapid(str(path))[0])


def onnxtr_line(path):
    """Recognition-only PARSeq on a located line, avoiding the large detector."""
    global _onnxtr
    import numpy as np
    from PIL import Image
    if _onnxtr is None:
        import onnxruntime
        from onnxtr.models import EngineConfig, recognition_predictor
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads, options.inter_op_num_threads = 1, 1
        options.enable_cpu_mem_arena = False
        _onnxtr = recognition_predictor('parseq', batch_size=1, engine_cfg=EngineConfig(session_options=options))
    with Image.open(path) as image:
        array = np.asarray(image.convert('RGB'))
    return [_onnxtr([array])[0][0]]


def luna_line(path):
    # Only explicitly requested disagreement crops go to the vision model; no
    # candidate spelling is supplied. Its answer is review evidence, not a vote.
    prompt = ('Read the image at ' + str(Path(path).resolve()) + ' using the image tool. Transcribe its one printed line. '
              'Do not infer covered/redacted text. Reply ONLY JSON {"line":"...","legible":true}; '
              'use legible:false for an unreadable line. Do not read other files or use subagents.')
    result = subprocess.run(['sol', '-m', 'gpt-6-luna', prompt], capture_output=True, text=True, timeout=150, check=True)
    answer = json.loads(result.stdout.strip().removeprefix('```json').removesuffix('```').strip())
    return [answer['line']] if answer.get('legible') else []


def page_cache_path(doc, page, dpi, pdf, cache=CACHE):
    # Size, not mtime: batches delete PDFs after reading, and a re-download of
    # the same DOJ file must keep its cached readings.
    if pdf is None:  # fully cached page: no download, newest cache file
        older = sorted(Path(cache).glob(f'{doc}_{page}_????????????.json'), key=lambda p: p.stat().st_mtime)
        return older[-1] if older else Path(cache) / f'{doc}_{page}_uncached.json'
    signature = hashlib.sha256(f'{VERSION}:{dpi}:{Path(pdf).stat().st_size}:size'.encode()).hexdigest()[:12]
    path = Path(cache) / f'{doc}_{page}_{signature}.json'
    if not path.exists():
        older = sorted(Path(cache).glob(f'{doc}_{page}_????????????.json'), key=lambda p: p.stat().st_mtime)
        if older:
            return older[-1]  # earlier mtime-keyed cache of the same page
    return path


def fully_cached(doc, page_entries, cache=CACHE):
    """True when every planned line of every page already has a cached crop reading."""
    for page, entries in page_entries:
        path = page_cache_path(doc, page, None, None, cache)
        if not path.exists():
            return False
        try:
            lines = json.loads(path.read_text()).get('lines', {})
        except ValueError:
            return False
        if any(entry['corpus_line'] not in lines for _, entry in entries):
            return False
    return True


def read_page(job):
    """Bounded crop OCR; cached once per page/line/model settings, errors retry."""
    pdf, plans, dpi, extra, luna, cache_dir = job
    from PIL import Image
    doc, page = plans[0]['doc_id'], plans[0]['page']
    path = page_cache_path(doc, page, dpi, pdf, cache_dir)
    saved = json.loads(path.read_text()) if path.exists() else {'version': VERSION, 'lines': {}}
    pending = [p for p in plans if p['corpus_line'] not in saved['lines']]
    if pending:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = str(Path(tmp) / 'p')
            subprocess.run(['pdftoppm', '-f', str(page + 1), '-l', str(page + 1), '-r', str(dpi),
                            '-scale-to', '2200', '-gray', '-singlefile', str(pdf), prefix],
                           check=True, capture_output=True, timeout=60)
            doj = subprocess.run(['pdftotext', '-layout', '-f', str(page + 1), '-l', str(page + 1), str(pdf), '-'],
                                 check=True, capture_output=True, text=True, timeout=60).stdout
            rows = tsv_rows(tesseract(prefix + '.pgm', tsv=True))
            with Image.open(prefix + '.pgm') as image:
                for plan in pending:
                    line = plan['corpus_line']
                    box = crop_box(rows, line, *image.size)
                    if box is None:
                        continue  # not a successful empty reading: retry on rerun
                    token = hashlib.sha256(line.encode()).hexdigest()[:16]
                    crop = path.with_name(path.stem + '_' + token + '.png')
                    crop.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    # Crop permissions apply before writing pixels.
                    fd = os.open(crop, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                    with os.fdopen(fd, 'wb') as stream:
                        image.crop(box).save(stream, format='PNG')
                    text = tesseract(crop)
                    rapid = rapid_lines(crop)
                    saved['lines'][line] = {'box': box, 'crop': str(crop),
                        'readings': {'doj': doj.splitlines(), 'tesseract': text.splitlines(), 'rapidocr': rapid}}
                    private_json(path, saved)
    checks = []
    for plan in plans:
        entry = saved['lines'].get(plan['corpus_line'])
        if entry is None:
            checks.append({**plan, 'verdict': 'line_not_matched', 'readings': {}, 'consensus': None, 'engines': [], 'agreement': 0})
            continue
        readings = {}
        def extract(engine):
            hit = matched_line('\n'.join(entry['readings'].get(engine, [])), plan['corpus_line'], threshold=48)
            line = hit['line'] if hit else ''
            readings[engine] = {'line': line, 'spelling': reading_span(plan['raw'], plan['corpus_line'], line),
                                'match': hit['match'] if hit else 0}
        for engine in BASE_ENGINES:
            extract(engine)
        disagrees = len({spelling_key(r['spelling']) for r in readings.values() if r['spelling']}) > 1
        for engine, enabled, reader in [('onnxtr', extra, onnxtr_line), ('luna', luna, luna_line)]:
            if enabled and disagrees:
                if engine not in entry['readings']:
                    try:
                        entry['readings'][engine] = reader(entry['crop'])
                        private_json(path, saved)
                    except Exception as error:
                        readings[engine] = {'line': '', 'spelling': None, 'error': type(error).__name__}
                        continue
                extract(engine)
        original = reading_span(plan['raw'], plan['corpus_line'], plan['corpus_line'])
        checks.append({**plan, 'corpus_spelling': original, 'readings': readings, 'crop': entry['crop'], 'box': entry['box'],
                       **vote(plan['raw'], readings, original)})
    return {'checks': checks, 'peak_worker_mb': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)}


def run_bounded(jobs, workers):
    """At most workers page futures; kill an oversized worker tree at 580 MiB.

    Max one page per fresh process also prevents ONNX arenas accumulating.
    The parent adds ~100 MiB; three bounded workers fit within ~1.5 GiB in the
    measured pilot. A memory-limit failure is retriable and never cached.
    """
    import psutil
    with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context('spawn'),
                             initializer=initialize_worker, max_tasks_per_child=1) as pool:
        todo, inflight = iter(jobs), {}
        def fill():
            while len(inflight) < workers:
                job = next(todo, None)
                if job is None:
                    break
                inflight[pool.submit(read_page, job)] = job
        fill()
        while inflight:
            done, _ = wait(inflight, timeout=1, return_when=FIRST_COMPLETED)
            total_rss = psutil.Process().memory_info().rss
            for worker in list(pool._processes.values()):
                try:
                    proc = psutil.Process(worker.pid)
                    children = proc.children(recursive=True)
                    rss = sum(p.memory_info().rss for p in [proc, *children] if p.is_running())
                    total_rss += rss
                    if rss > 580 * 1024 ** 2:
                        for child in children:
                            child.kill()
                        proc.kill()
                        raise RuntimeError('OCR worker exceeded 580 MiB; rerun with --workers 1, without --onnxtr')
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if total_rss > 1400 * 1024 ** 2:
                for worker in list(pool._processes.values()):
                    try:
                        proc = psutil.Process(worker.pid)
                        for child in proc.children(recursive=True):
                            child.kill()
                        proc.kill()
                    except psutil.NoSuchProcess:
                        pass
                raise RuntimeError('OCR pool exceeded 1400 MiB; rerun with --workers 1')
            for future in done:
                job = inflight.pop(future)
                try:
                    yield job, future.result(), None
                except Exception as error:
                    yield job, None, type(error).__name__
            fill()


def summary(report):
    checks = [c for n in report for c in n.get('checks', [])]
    aliases = consensus_map(report)
    return {'nodes': len(report), 'nodes_read': sum(bool(n.get('checks')) for n in report), 'node_page_checks': len(checks),
            'nodes_matched': sum(any(c['verdict'] != 'line_not_matched' for c in n.get('checks', [])) for n in report),
            'matched_checks': sum(c['verdict'] != 'line_not_matched' for c in checks),
            'unique_pages': len({c['page_id'] for c in checks}), 'verdicts': dict(Counter(c['verdict'] for c in checks)),
            'agreeing_checks': sum(len(c.get('engines', [])) >= 2 for c in checks),
            'three_engine_agreement': sum(len(c.get('engines', [])) >= 3 for c in checks),
            'variants_resolved': len(aliases['variants']),
            'conflicting_spellings_withheld': aliases['conflicting_spellings_withheld']}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--nodes-from', type=Path, default=ROOT / 'data/private/audit/viewer_nodes.json')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--kinds', nargs='+', help='viewer kinds or email/company/entity; comma separated also accepted')
    parser.add_argument('--names', nargs='+', help='explicit substring matches, including clean controls')
    parser.add_argument('--all-nodes', action='store_true', help='check every node in an explicitly curated input list')
    parser.add_argument('--pilot', action='store_true', help='up to 20 family examples, then seeded random suspects')
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--pages', type=int, choices=(1, 2, 3), default=3)
    parser.add_argument('--workers', type=int, choices=(1, 2, 3), default=3)
    parser.add_argument('--dpi', type=int, default=240)
    parser.add_argument('--pause', type=float, default=0.5)
    parser.add_argument('--batch-docs', type=int, default=200, help='download, read, then delete this many PDFs at a time')
    parser.add_argument('--keep-pdfs', action='store_true', help='keep PDFs downloaded by this run')
    parser.add_argument('--cached-only', action='store_true')
    parser.add_argument('--plan-only', action='store_true', help='locate pages without OCR/downloads')
    parser.add_argument('--onnxtr', action='store_true', help='PARSeq line recognition for engine disagreements')
    parser.add_argument('--luna', action='store_true', help='vision tie-breaker evidence only; never an automatic vote')
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error('--limit must be nonnegative')
    os.umask(0o077)
    nodes = choose_nodes(json.loads(args.nodes_from.read_text()), args)
    plans = source_pages(nodes, per_node=args.pages, cached_only=args.cached_only)
    previous = json.loads(OUT.read_text()) if OUT.exists() else {'nodes': []}
    if previous.get('version') != VERSION:
        previous = {'nodes': []}
    report = {str(n['id']): n for n in previous.get('nodes', [])}
    for node in nodes:
        identity = str(node['id'])
        old = report.get(identity, {})
        checks = old.get('checks', []) if old.get('raw') == label(node) else []
        report[identity] = {'id': node['id'], 'raw': label(node), 'kind': kind(node), 'reasons': node['reasons'],
                            'peers': node['peers'], 'plans': plans[identity], 'checks': checks,
                            'status': 'planned' if plans[identity] else 'no_source_page'}
    peak = previous.get('metrics', {}).get('peak_worker_mb', 0)
    def save():
        values = list(report.values())
        result = summary(values)
        private_json(OUT, {'version': VERSION, 'settings': vars(args) | {'nodes_from': str(args.nodes_from)},
                           'summary': result, 'metrics': {'peak_worker_mb': peak, 'worker_limit_mb': 580, 'pool_limit_mb': 1400},
                           'nodes': values})
        private_json(CONSENSUS, consensus_map(values))
        return result
    if args.plan_only:
        # Do not replace the accumulated review/aliases with an unexecuted plan.
        private_json(PRIVATE / 'node_plan.json', {'nodes': [report[str(n['id'])] for n in nodes]})
        print(json.dumps({'nodes': len(nodes), 'located': sum(bool(p) for p in plans.values()),
                          'pages': sum(len(p) for p in plans.values())}), flush=True)
        return
    import corpus
    pages = defaultdict(list)
    for identity, entries in plans.items():
        for entry in entries:
            pages[(entry['doc_id'], entry['page'])].append((identity, entry))
    docs = {d for d, _ in pages}
    # document_urls materializes the source CSV; call it before spawning OCR
    # workers and discard it afterwards. Cached-only pilot needs no URLs.
    urls = corpus.document_urls(docs) if not args.cached_only else {}
    downloader = Downloader(args.pause)
    by_doc = defaultdict(list)
    for (doc, page), entries in pages.items():
        by_doc[doc].append((page, entries))
    print(json.dumps({'selected_nodes': len(nodes), 'located_nodes': sum(bool(p) for p in plans.values()),
                      'ocr_pages': len(pages), 'documents': len(by_doc)}), flush=True)
    save()
    # Download, read and delete in batches: the full selection is ~20 GB of
    # PDFs, more than the free disk. PDFs that were cached before stay.
    count, last_save, unavailable = 0, time.monotonic(), 0
    ordered = sorted(by_doc)
    for start in range(0, len(ordered), args.batch_docs):
        jobs, fetched = [], []
        for doc in ordered[start:start + args.batch_docs]:
            had = (WORK / f'{doc}.pdf').exists()
            if not had and fully_cached(doc, by_doc[doc]):
                for page, entries in by_doc[doc]:
                    jobs.append((None, [p for _, p in entries], args.dpi, args.onnxtr, args.luna, str(CACHE)))
                continue
            try:
                pdf = downloader.get(doc, urls.get(doc), args.cached_only)
            except Exception as error:
                pdf = None
                print(f'{doc}: download {type(error).__name__}', flush=True)
            if pdf and not had:
                fetched.append(pdf)
            for page, entries in by_doc[doc]:
                if pdf:
                    jobs.append((str(pdf), [p for _, p in entries], args.dpi, args.onnxtr, args.luna, str(CACHE)))
                else:
                    unavailable += 1
                    for identity, _ in entries:
                        report[identity]['status'] = 'pdf_unavailable'
        for job, result, error in run_bounded(jobs, args.workers):
            count += 1
            entries = pages[(job[1][0]['doc_id'], job[1][0]['page'])]
            for index, (identity, _) in enumerate(entries):
                if error:
                    report[identity]['status'] = 'ocr_error:' + error
                else:
                    new = result['checks'][index]
                    report[identity]['checks'] = [c for c in report[identity]['checks'] if c['page_id'] != new['page_id']] + [new]
                    report[identity]['status'] = 'read'
            peak = max(peak, result['peak_worker_mb'] if result else 0)
            if time.monotonic() - last_save > 120:
                save()
                last_save = time.monotonic()
        if not args.keep_pdfs:
            for pdf in fetched:
                Path(pdf).unlink(missing_ok=True)
        print(f'{count}/{len(pages)} pages, {min(start + args.batch_docs, len(ordered))}/{len(ordered)} documents; '
              f'{unavailable} pages without PDF; peak worker {peak} MiB', flush=True)
    del urls
    print(json.dumps(save() | {'peak_worker_mb': peak}, indent=1))


if __name__ == '__main__':
    main()
