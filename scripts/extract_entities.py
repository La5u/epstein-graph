"""Resumable, bounded-memory full-corpus NER, gazetteer and website mentions.

Raw uncurated people/weak tokens stay in data/private; the public mention table
uses the same HMAC namespace as email-header participants. No model calls.
"""
import argparse
from collections import Counter
import ctypes
import ctypes.util
import hashlib
import json
from multiprocessing import get_context
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

import corpus
from build_communications import Builder
from build_contacts import contact_key, contact_id
from build_companies import normal_name
from normalize_graph import name_key
from entity_labels import RULES_PATH, cannot_be_name, is_markup
from entity_resolution import (read_nodes, ExistingIndex, person_kind, EMAIL_LIKE,
                               email_address, FIRST_NAMES_PATH)
from build_communications import ocr_repair

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/.build/entity_mentions.db'
PRIVATE = ROOT / 'data/private/entity_spellings.db'
GAZETTEER = ROOT / 'config/entity_gazetteer.json'
LABELS = {'PERSON', 'ORG', 'GPE', 'LOC', 'FAC', 'NORP', 'PRODUCT', 'LAW', 'WORK_OF_ART'}
EMAIL = re.compile(r'[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}')
HEADER = re.compile(r'\b(?:From|To|Cc|Sent|Subject|Date|mailto)\s*:?\s*', re.I)
DOMAIN = re.compile(r'(?<![\w@.-])(?:https?://)?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:[a-z]{2,24})(?::\d+)?(?:/[^\s<>"\x27]*)?', re.I)
OCR_DOMAIN = re.compile(r'\b([A-Za-z0-9-]+)\s+(com|org|net|gov|edu|co\.uk)\b', re.I)
CAPITAL = re.compile(r'\b[A-Z][A-Za-z\x27-]+\b')


def clean_span(text):
    text = text.split('\n', 1)[0]
    text = EMAIL.sub('', text)
    text = HEADER.sub('', text)
    text = re.sub(r'^\s*the\s+', '', text, flags=re.I)
    if is_markup(text):
        return ' '.join(text.split())
    text = ' '.join(text.split()).strip(' \t,.;:!?<>[](){}"\x27-')
    return text if text and not cannot_be_name(text) else None


_psl = None

def domain_key(value):
    global _psl
    if _psl is None:
        _psl = ctypes.CDLL(ctypes.util.find_library('psl') or 'libpsl.so.5')
        _psl.psl_is_public_suffix2.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        _psl.psl_is_public_suffix2.restype = ctypes.c_int
        _psl.psl_builtin.restype = ctypes.c_void_p
        _psl.psl_registrable_domain.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        _psl.psl_registrable_domain.restype = ctypes.c_char_p
    host = urlsplit(value if '://' in value else 'https://' + value).hostname
    if not host:
        return None
    host = host.lower().removeprefix('www.').rstrip('.')
    if not _psl.psl_is_public_suffix2(_psl.psl_builtin(), host.rsplit('.', 1)[-1].encode(), 7):
        return None
    result = _psl.psl_registrable_domain(_psl.psl_builtin(), host.encode())
    return result.decode() if result else None


def gazetteer_patterns(config):
    return [(name, spec, re.compile(r'(?<!\w)' + re.escape(alias) + r'(?!\w)',
                0 if alias.isupper() and len(alias) <= 5 else re.I))
            for name, spec in config.items() for alias in {name, *spec.get('aliases', [])}]


def extract(text, doc, dictionary, patterns, resolver=None, page_text=None):
    """Yield (canonical, label/kind, spelling, subtype, gazetteer); no identities inferred."""
    occupied = [(e.start_char, e.end_char) for e in doc.ents]
    emails = []
    for match in EMAIL_LIKE.finditer(text):
        address = email_address(match.group())
        if address:
            emails.append((match.start(), match.end()))
            yield '@'.join(ocr_repair(address)), 'email', match.group(), '', 0
    gaz_spans = []
    for canonical, spec, pattern in patterns:
        for m in pattern.finditer(text):
            if any(a < m.end() and b > m.start() for a, b in emails):
                continue
            gaz_spans.append((m.start(), m.end()))
            yield canonical, 'gazetteer', m.group(), spec['subtype'], 1
    sites = []
    for regex in (DOMAIN, OCR_DOMAIN):
        for m in regex.finditer(text):
            if any(a < m.end() and b > m.start() for a, b in emails):
                continue
            spelling = m.group().rstrip('.,;:!?)')
            value = '.'.join(m.groups()) if regex is OCR_DOMAIN else spelling
            key = domain_key(value)
            if key:
                sites.append((m.start(), m.end()))
                yield key, 'website', spelling, '', 0
    anchors = {normal_name(e.text) for e in doc.ents}
    normalized_page = None
    for ent in doc.ents:
        if ent.label_ not in LABELS or any(a <= ent.start_char and b >= ent.end_char for a, b in gaz_spans + sites + emails):
            continue
        value = clean_span(ent.text)
        if value:
            # NER can split a full legal name elsewhere on this same page.
            # Recover only exact normalized full names for observed tail spans.
            if resolver and normal_name(value) in resolver.tail_names:
                if normalized_page is None:
                    normalized_page = ' ' + normal_name(page_text if page_text is not None else text) + ' '
                for full in resolver.tail_names[normal_name(value)]:
                    if full not in anchors and ' ' + full + ' ' in normalized_page:
                        anchors.add(full)
                        label = resolver.full_labels[full]
                        hits = resolver.full_names[full] & resolver.hits(label)
                        identity = resolver.choose(hits)
                        if identity is not None:
                            anchor_kind = 'GPE' if resolver.existing[identity]['entity_type'] == 'location' else 'ORG'
                            yield label, anchor_kind, label, '', 0
            known = bool(resolver and any(resolver.existing[i]['entity_type'] in {'location','organization','shell_company'} for i in resolver.hits(value)))
            known = known or any(pattern.fullmatch(value) for _, _, pattern in patterns)
            kind = 'email' if '@' in value or email_address(value) else person_kind(value, ent.label_, known)
            yield value, kind, value, '', 0
    for m in CAPITAL.finditer(text):
        if m.group().casefold() not in dictionary and not any(a <= m.start() < b for a, b in occupied + gaz_spans + sites + emails):
            value = clean_span(m.group())
            if value:
                yield value, person_kind(value, 'unknown_word'), value, '', 0


_nlp = _corpus = _patterns = _dictionary = _builder = _resolver = None

def _init(path, key):
    global _nlp, _corpus, _patterns, _dictionary, _builder, _resolver
    import spacy
    _nlp = spacy.load('en_core_web_sm', disable=['tagger', 'parser', 'lemmatizer', 'attribute_ruler'])
    _nlp.max_length = 2000000
    _corpus = corpus.connect(Path(path))
    if _resolver is None:  # normally preloaded in the parent and inherited through fork
        _preload(key)


def _preload(key):
    """Shared read-only state, built once: recycled workers inherit it instead of
    re-reading ~100 MB of graph JSON each time."""
    global _patterns, _dictionary, _builder, _resolver
    _patterns = gazetteer_patterns(json.loads(GAZETTEER.read_text()))
    words = Path('/usr/share/dict/words')
    _dictionary = {w.casefold() for w in words.read_text().splitlines()} if words.exists() else set()
    _builder = Builder(key, [])
    _resolver = ExistingIndex(read_nodes(ROOT / 'output'), json.loads(GAZETTEER.read_text()))


def process(ids):
    rows, private = [], []
    for page_id in ids:
        doc_id, text = _corpus.execute('SELECT doc_id,text FROM pages WHERE page_id=?', (page_id,)).fetchone()
        text = text or ''
        # Process the entire page in bounded pieces, including pathological OCR pages.
        for offset in range(0, len(text), 100000):
            part = text[offset:offset + 100000]
            found = Counter(extract(part, _nlp(part), _dictionary, _patterns, _resolver, text))
            for (canonical, kind, spelling, subtype, gaz), n in found.items():
                key = canonical if kind == 'website' else normal_name(canonical)
                if kind == 'email':
                    key = contact_id(canonical, 'email', _builder.key)
                    private.append((key, spelling, n))
                    spelling = 'Email_' + key.split('_', 1)[1]
                elif kind in ('PERSON', 'person_weak', 'unknown_word'):
                    key = _builder.participant_id(name_key(canonical))
                    private.append((key, spelling, n))
                    spelling = 'HeaderLabel_' + key.split('_', 1)[1]
                rows.append((key, kind, spelling, page_id, doc_id, n, subtype, gaz))
    return ids, rows, private


def sample_documents(path):
    """A JSON array or newline-separated document IDs; stable across resumes."""
    text = path.read_text().strip()
    values = json.loads(text) if text.startswith('[') else text.splitlines()
    if not isinstance(values, list) or not values or not all(isinstance(v, str) and v.strip() for v in values):
        raise ValueError('sample-docs must contain a nonempty list of document IDs')
    return sorted({v.strip() for v in values})


def pending_pages(source, output, docs, limit):
    source.execute('ATTACH DATABASE ? AS progress', (str(output),))
    condition = ''
    if docs is not None:
        source.execute('CREATE TEMP TABLE sample_docs(id TEXT PRIMARY KEY)')
        source.executemany('INSERT INTO sample_docs VALUES (?)', ((d,) for d in docs))
        missing = source.execute('SELECT id FROM sample_docs WHERE id NOT IN (SELECT doc_id FROM pages WHERE doc_id IN (SELECT id FROM sample_docs))').fetchall()
        if missing:
            raise ValueError(f'{len(missing)} sampled documents have no corpus pages: {missing[:5]}')
        condition = ' AND doc_id IN (SELECT id FROM sample_docs)'
    return source.execute('SELECT page_id,length(coalesce(text,\'\')) FROM pages WHERE page_id NOT IN (SELECT page_id FROM progress.done)'
                          + condition + ' ORDER BY page_id LIMIT ?', (limit if limit is not None else -1,))


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--workers', type=int, default=3)
    p.add_argument('--limit-pages', type=int)
    p.add_argument('--corpus', type=Path, default=corpus.PATH)
    p.add_argument('--output', type=Path, default=OUT)
    p.add_argument('--sample-docs', type=Path, help='JSON array or newline-separated doc IDs; requires a separate --output')
    p.add_argument('--private-output', type=Path, help='Private spelling DB (sample mode uses its own DB by default)')
    args = p.parse_args()
    docs = sample_documents(args.sample_docs) if args.sample_docs else None
    if docs is not None and args.output.resolve() == OUT.resolve():
        p.error('--sample-docs requires a separate --output; the resumable corpus DB is protected')
    private_path = args.private_output or (PRIVATE.parent / ('sample_' + hashlib.sha256(str(args.output.resolve()).encode()).hexdigest()[:12] + '_spellings.db') if docs is not None else PRIVATE)
    if docs is not None and private_path.resolve() == PRIVATE.resolve():
        p.error('--sample-docs requires a separate private spelling DB')
    if private_path.resolve() == args.output.resolve():
        p.error('--private-output and --output must differ')
    key = contact_key()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.output)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA cache_size=-16000')
    db.executescript('''CREATE TABLE IF NOT EXISTS mentions(key TEXT, kind TEXT, spelling TEXT, page_id INTEGER,
        doc_id TEXT, n INTEGER, subtype TEXT, gazetteer INTEGER);
        CREATE TABLE IF NOT EXISTS done(page_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS settings(signature TEXT);''')
    # Code/rules/corpus changes invalidate stored mentions. Graph content does not: upstream node
    # files change on most builds, so including them would wipe a 4-5 h extraction each rebuild.
    # A node change is only logged; rerun with a fresh --output to re-resolve against new nodes.
    signature = hashlib.sha256(GAZETTEER.read_bytes() + RULES_PATH.read_bytes() + FIRST_NAMES_PATH.read_bytes()
                               + (ROOT / 'scripts/entity_resolution.py').read_bytes()
                               + (ROOT / 'scripts/entity_labels.py').read_bytes() + Path(__file__).read_bytes()
                               + str(args.corpus.resolve()).encode() + json.dumps(docs).encode()).hexdigest()
    nodes_hash = hashlib.sha256(json.dumps(sorted(read_nodes(ROOT / 'output').values(), key=lambda n: str(n['id'])),
                                           sort_keys=True, default=str).encode()).hexdigest()
    db.execute('CREATE TABLE IF NOT EXISTS node_inputs(hash TEXT)')
    old = db.execute('SELECT signature FROM settings').fetchone()
    if old and old[0] != signature:
        db.executescript('DELETE FROM mentions; DELETE FROM done; DELETE FROM settings; DELETE FROM node_inputs;')
    if not db.execute('SELECT 1 FROM settings').fetchone():
        db.execute('INSERT INTO settings VALUES (?)', (signature,))
    old_nodes = db.execute('SELECT hash FROM node_inputs').fetchone()
    if old_nodes and old_nodes[0] != nodes_hash:
        print('note: graph nodes changed since stored mentions were extracted; keeping them', flush=True)
    if not old_nodes:
        db.execute('INSERT INTO node_inputs VALUES (?)', (nodes_hash,))
    db.commit()
    private_path.touch(mode=0o600, exist_ok=True)
    private_path.chmod(0o600)
    private = sqlite3.connect(private_path)
    private.execute('CREATE TABLE IF NOT EXISTS spellings(key TEXT, spelling TEXT, n INTEGER, PRIMARY KEY(key,spelling))')
    source = corpus.connect(args.corpus)
    # Materialize the pending list: a cursor left open on the attached output DB
    # pins a read snapshot, so the WAL could never checkpoint (grew past 1.3 GB).
    import numpy as np
    pending = np.array(pending_pages(source, args.output, docs, args.limit_pages).fetchall() or np.empty((0, 2)), dtype=np.int64).reshape(-1, 2)
    source.execute('DETACH DATABASE progress')
    # Keep only a small window of tasks in flight; Pool's feeder must not buffer the corpus.
    started, count, characters = time.perf_counter(), 0, 0
    # Recycle workers: spaCy's string store grows without bound over long runs
    # (a worker reached 1.6 GB after ~380k pages and was OOM-killed).
    step = 100 * max(1, args.workers)
    _preload(key)
    with get_context("fork").Pool(max(1, args.workers), initializer=_init, initargs=(str(args.corpus), key),
                                  maxtasksperchild=200) as pool:
        for start in range(0, len(pending), step):
            batch = pending[start:start + step].tolist()
            chunks = [[r[0] for r in batch[i:i + 100]] for i in range(0, len(batch), 100)]
            for ids, rows, raw in pool.imap_unordered(process, chunks):
                private.executemany('INSERT INTO spellings VALUES (?,?,?) ON CONFLICT(key,spelling) DO UPDATE SET n=max(n,excluded.n)', raw)
                private.commit()
                db.executemany('INSERT INTO mentions VALUES (?,?,?,?,?,?,?,?)', rows)
                db.executemany('INSERT INTO done VALUES (?)', ((i,) for i in ids))
                db.commit()
                count += len(ids)
            characters += sum(r[1] for r in batch)
            print(f'{count} pages, {count / (time.perf_counter()-started):.1f} pages/s', flush=True)
    db.executescript('CREATE INDEX IF NOT EXISTS mentions_key ON mentions(key,doc_id); CREATE INDEX IF NOT EXISTS mentions_doc ON mentions(doc_id);')
    db.execute('CREATE VIEW IF NOT EXISTS counts AS SELECT key,kind,sum(n) AS occurrences,count(DISTINCT page_id) AS pages,count(DISTINCT doc_id) AS documents FROM mentions GROUP BY key,kind')
    db.commit()
    seconds = max(time.perf_counter() - started, .001)
    metrics = {'pages': count, 'characters': characters, 'seconds': round(seconds, 3),
               'pages_per_second': round(count / seconds, 2), 'chars_per_second': round(characters / seconds, 2),
               'sample_documents': len(docs) if docs is not None else None, 'timer': 'perf_counter',
               'private_output': str(private_path), 'signature': signature}
    args.output.with_suffix('.metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    print(json.dumps(metrics))
    db.close(); private.close(); source.close()


if __name__ == '__main__':
    main()
