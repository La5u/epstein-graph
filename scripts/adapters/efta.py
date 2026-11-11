"""EFTA adapter: the DOJ Epstein Files release as rebuilt upstream (rhowardstone,
data/raw/full_text_corpus.db; document ids are EFTA numbers). Reads the file in
place: TEMP views give its tables the common column names, nothing is copied.

Official URLs come from the pinned VERIFICATION_URLS.csv.gz; documents it lacks
(DOJ court-record sections, late DataSet 12 files) fall back to the corpus's own
official URL or DataSet path.
"""
import bisect
import csv
import gzip
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / 'data/raw/full_text_corpus.db'
URLS = ROOT / 'data/raw/VERIFICATION_URLS.csv.gz'
SOURCE = 'efta'
DOJ_FILES = 'https://www.justice.gov/epstein/files/'
TRUSTED = ('https://www.justice.gov/', 'https://archive.org/')
TEXT_COLUMN = 'text_content'  # the pages_fts column holding page text
EFTA = re.compile(r'\bEFTA\d{8}\b')
META = ('dataset', 'file_path', 'total_pages', 'error', 'stamp_type', 'sender')


def connect(path=PATH):
    """Read-only connection to the corpus file (the build traces it as an input)."""
    return sqlite3.connect(f'file:{path}?mode=ro&immutable=1', uri=True, check_same_thread=False)


def _columns(db, table):
    return {row[1] for row in db.execute(f'PRAGMA main.table_info({table})')}


def install_views(db):
    """Common-format TEMP views over the legacy tables. They shadow main.documents and
    main.pages for unqualified names; tables or columns a (test) file lacks read as NULL.

    type is the upstream classifier's (document_classification; documents.doc_type is
    mostly empty), date the email date in doc_search, meta.redactions the redaction
    count in efta_crosswalk."""
    docs, pages = _columns(db, 'documents'), _columns(db, 'pages')
    col = lambda have, name, alias='d': f'{alias}.{name}' if name in have else 'NULL'
    if docs:
        joins = {'c': 'document_classification', 's': 'doc_search', 'x': 'efta_crosswalk'}
        have = {alias: _columns(db, table) for alias, table in joins.items()}
        meta = ', '.join([f"'{m}', {col(docs, m)}" for m in META] + [f"'redactions', {col(have['x'], 'redaction_total', 'x')}"])
        db.execute(f'''CREATE TEMP VIEW documents AS SELECT d.efta_number AS id, '{SOURCE}' AS source, NULL AS title,
            {col(docs, 'pdf_url')} AS url, {col(have['s'], 'email_date', 's')} AS date,
            {col(have['c'], 'doc_type', 'c')} AS type, json_object({meta}) AS meta FROM main.documents d '''
                   + ' '.join(f'LEFT JOIN main.{table} {alias} ON {alias}.efta_number = d.efta_number'
                              for alias, table in joins.items() if have[alias]))
    if pages:
        db.execute(f'''CREATE TEMP VIEW pages AS SELECT {'id' if 'id' in pages else 'rowid'} AS page_id,
            efta_number AS doc_id, {col(pages, 'page_number', 'p')} AS page, {col(pages, 'text_content', 'p')} AS text
            FROM main.pages p''')


def create(db, pages, documents=()):
    """Write a minimal file in this source's layout (test fixtures): pages
    [(doc_id, page, text)] get page_ids 1.. in order; documents [(doc_id, total_pages)]."""
    db.executescript('''CREATE TABLE documents(id INTEGER PRIMARY KEY, efta_number TEXT NOT NULL UNIQUE, total_pages INTEGER,
                                                pdf_url TEXT, file_path TEXT);
        CREATE TABLE pages(id INTEGER PRIMARY KEY, efta_number TEXT NOT NULL, page_number INTEGER NOT NULL, text_content TEXT);
        CREATE VIRTUAL TABLE pages_fts USING fts5(efta_number, page_number UNINDEXED, text_content,
                                                  content=pages, content_rowid=id);''')
    db.executemany('INSERT INTO documents(efta_number, total_pages) VALUES (?, ?)', documents)
    db.executemany('INSERT INTO pages(efta_number, page_number, text_content) VALUES (?, ?, ?)', pages)
    db.execute("INSERT INTO pages_fts(pages_fts) VALUES ('rebuild')")
    db.commit()


def scope(query):
    """An FTS5 query over page text, restricted to this file's text column."""
    return f'{TEXT_COLUMN}:({query})'


def document_urls(ids, path=PATH, urls=URLS):
    """{id: official url} for the requested documents (empty without the URL list)."""
    wanted, found = set(ids), {}
    if not wanted or not urls.exists():
        return found
    with gzip.open(urls, 'rt', newline='') as f:
        text = f.read()
    if '"' in text:  # quoted fields can span lines; take the general path
        rows = csv.DictReader(text.splitlines())
    else:
        # 1.4M rows: match the leading id before paying for csv parsing.
        lines = text.splitlines()
        header = next(csv.reader(lines[:1]))
        id_column = header.index('document_id')
        rows = csv.DictReader([line for line in lines[1:]
                               if line.split(',', id_column + 1)[id_column] in wanted], fieldnames=header)
    for row in rows:
        if row['document_id'] in wanted and row['verification_url'].startswith(TRUSTED):
            found[row['document_id']] = row['verification_url']
    found.update(corpus_urls(wanted - set(found), path))
    return found


def corpus_urls(ids, path=PATH):
    """{id: url} from the corpus documents table (official URLs only)."""
    found = {}
    if not ids or not path.exists():
        return found
    with closing(sqlite3.connect(f'file:{path}?mode=ro&immutable=1', uri=True)) as db:
        for efta in sorted(ids):
            row = db.execute('SELECT file_path, pdf_url FROM documents WHERE efta_number=?', (efta,)).fetchone()
            if not row:
                continue
            file_path, url = row
            if url and url.startswith(TRUSTED):
                found[efta] = url
            elif (file_path or '').startswith('DataSet ') and file_path.endswith('.pdf'):
                found[efta] = DOJ_FILES + quote(file_path)
    return found


def cited_ids(value):
    """EFTA numbers anywhere in a record (efta, efta_numbers, sample lists, notes)."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value[:1] in '{[' else value
        except ValueError:
            pass
    if isinstance(value, str):
        return EFTA.findall(value)
    if isinstance(value, dict):
        return [e for item in value.values() for e in cited_ids(item)]
    if isinstance(value, list):
        return [e for item in value for e in cited_ids(item)]
    return []


def page_locator(db):
    """efta -> (document, 0-based page). EFTA numbers are per-page Bates numbers, and records
    often cite an inner page (EFTA01660636 is page 15 of EFTA01660622); documents map to page 0."""
    starts = names = sizes = None

    def locate(efta):
        nonlocal starts, names, sizes
        if db is None or not EFTA.fullmatch(efta):
            return efta, None
        if starts is None:
            try:
                rows = sorted((int(e[4:]), e, n or 1) for e, n in db.execute(
                    "SELECT efta_number, total_pages FROM main.documents WHERE efta_number GLOB 'EFTA[0-9]*'") if EFTA.fullmatch(e))
            except sqlite3.OperationalError:  # a corpus without the documents table
                rows = []
            starts, names, sizes = [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]
        number = int(efta[4:])
        i = bisect.bisect_right(starts, number) - 1
        if i >= 0 and number - starts[i] < sizes[i]:
            return names[i], number - starts[i]
        return efta, None
    return locate

