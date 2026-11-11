"""PRIVATE, local-only: places mentioned in email bodies (spaCy NER + street addresses).

    .venv/bin/python scripts/extract_places.py [--workers 3] [--limit N]

Reads email documents (type email / email.header)
through the common corpus format, first MAX_PAGES pages each, and records per
document:
  GPE / LOC / FAC  spaCy en_core_web_sm entities (cities, countries, regions,
                   named buildings and facilities); noisy, filtered downstream
  STREET           US/UK-style street addresses ("575 Lexington Avenue")
Writes data/private/email_places.db (mentions(efta, kind, text, n)); street
addresses can be private homes, so this stays under data/private/. Resumable:
documents already in done(efta) are skipped.
"""
import argparse
import re
import sqlite3
import time
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

import corpus as corpus_source
from place_normalization import clean_place_text, place_label_reason

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data/private/email_places.db'
MAX_PAGES, MAX_CHARS, CHUNK = 5, 20000, 200
STREET = re.compile(r'\b\d{1,5}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][a-z]+\.?\s+){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|'
                    r'Place|Pl|Court|Ct|Terrace|Highway|Hwy|Parkway|Pkwy|Square|Sq|Mews|Gardens)\b\.?(?:,?\s+(?:Apt|Suite|Ste|#)\s*\w+)?')
_nlp = _corpus = None


def _init():
    global _nlp, _corpus
    import spacy
    _nlp = spacy.load('en_core_web_sm', disable=['parser', 'lemmatizer', 'tagger', 'attribute_ruler'])
    _nlp.max_length = MAX_CHARS + 1000
    _corpus = corpus_source.connect()


def extract(chunk):
    """[efta] -> (eftas, [(efta, kind, text, n)])."""
    texts = []
    for efta in chunk:
        pages = _corpus.execute('SELECT text FROM pages WHERE doc_id=? ORDER BY page LIMIT ?', (efta, MAX_PAGES))
        texts.append(' '.join(t or '' for (t,) in pages)[:MAX_CHARS])
    rows = []
    for efta, text, doc in zip(chunk, texts, _nlp.pipe(texts, batch_size=32)):
        found = Counter()
        for ent in doc.ents:
            if ent.label_ in ('GPE', 'LOC', 'FAC'):
                name = clean_place_text(ent.text)
                if 2 < len(name) < 60 and not place_label_reason(name):
                    found[(ent.label_, name)] += 1
        for match in STREET.finditer(text):
            name = ' '.join(match.group(0).split()).rstrip('.')
            if not place_label_reason(name):
                found[('STREET', name)] += 1
        rows += [(efta, kind, name, n) for (kind, name), n in found.items()]
    return chunk, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(OUT)
    out.executescript('CREATE TABLE IF NOT EXISTS mentions(efta TEXT, kind TEXT, text TEXT, n INTEGER);'
                      'CREATE TABLE IF NOT EXISTS done(efta TEXT PRIMARY KEY);')
    OUT.chmod(0o600)
    corpus = corpus_source.connect()
    done = {e for (e,) in out.execute('SELECT efta FROM done')}
    todo = [e for (e,) in corpus.execute("SELECT id FROM documents WHERE type IN ('email','email.header') ORDER BY id")
            if e not in done][:args.limit]
    print(f'{len(todo)} documents to read ({len(done)} already done)', flush=True)
    chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)]
    started, count = time.time(), 0
    with Pool(args.workers, initializer=_init) as pool:
        for chunk, rows in pool.imap_unordered(extract, chunks):
            out.executemany('INSERT INTO mentions VALUES (?,?,?,?)', rows)
            out.executemany('INSERT OR IGNORE INTO done VALUES (?)', [(e,) for e in chunk])
            out.commit()
            count += len(chunk)
            if count % 20000 < CHUNK:
                rate = count / (time.time() - started)
                print(f'{count}/{len(todo)} documents, ~{(len(todo) - count) / rate / 60:.0f} min left', flush=True)
    out.execute('CREATE INDEX IF NOT EXISTS mentions_text ON mentions(kind, text)')
    out.commit()
    print('done', flush=True)


if __name__ == '__main__':
    main()
