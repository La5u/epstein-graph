"""List corpus pages where names/phrases appear together, with text snippets.

    .venv/bin/python scripts/cooccur.py "Leon Black" "Jes Staley" [--near 50] [--limit 20]

Use it to read the documents behind a co-mention lead. Matching is exact-phrase
FTS5 (case-insensitive); --near limits matches to phrases within N tokens of
each other on the same page. Use --document to require all phrases in the same
document, possibly on different pages, with per-phrase snippets and provenance.
Each exact phrase must still occur within a page. --near is not supported in
document mode. Co-occurrence is not evidence of a relationship.
"""
import argparse
import re

import corpus
from adapters.efta import scope
from corpus import document_urls


def phrase(text):
    tokens = re.findall(r'\w+', text)
    if not tokens:
        raise SystemExit(f'No searchable words in {text!r}')
    return '"' + ' '.join(tokens) + '"'


def build_query(phrases, near):
    quoted = [phrase(p) for p in phrases]
    if near and len(quoted) > 1:
        return f'text_content:NEAR({" ".join(quoted)}, {near})'
    return ' AND '.join(f'text_content:{q}' for q in quoted)


def find_documents(db, phrases, limit):
    """Return total matching documents and limited results with phrase/page evidence."""
    queries = [scope(build_query([p], 0)) for p in phrases]
    # Intersect document IDs, not page IDs: every phrase needs its own exact hit.
    matches = ' INTERSECT '.join('''SELECT DISTINCT p.doc_id
        FROM main.pages_fts JOIN pages p ON p.page_id = pages_fts.rowid
        WHERE pages_fts MATCH ?''' for _ in queries)
    cte = f'WITH matches AS ({matches}) '
    total = db.execute(cte + 'SELECT count(*) FROM matches', queries).fetchone()[0]
    rows = db.execute(cte + '''SELECT m.doc_id, json_extract(d.meta, '$.dataset'), d.type, d.url
        FROM matches m LEFT JOIN documents d ON d.id = m.doc_id
        ORDER BY m.doc_id LIMIT ?''', (*queries, limit)).fetchall()
    results = []
    for efta, dataset, doc_type, url in rows:
        evidence = []
        for text, query in zip(phrases, queries):
            hits = db.execute('''SELECT p.page,
                    snippet(pages_fts, 2, '[', ']', ' … ', 24)
                FROM main.pages_fts JOIN pages p ON p.page_id = pages_fts.rowid
                WHERE pages_fts MATCH ? AND p.doc_id = ?
                ORDER BY p.page, p.page_id''', (query, efta)).fetchall()
            evidence.extend((text, page, snippet) for page, snippet in hits)
        results.append((efta, dataset, doc_type, url, evidence))
    return total, results


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('phrases', nargs='+', help='names or phrases that must all appear on the page (or document)')
    parser.add_argument('--document', action='store_true', help='verify all phrases in one document, including across pages')
    parser.add_argument('--near', type=int, default=None, help='require phrases within N tokens (0 = anywhere on page; page mode only)')
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    if args.document and args.near is not None:
        parser.error('--near cannot be used with --document (page proximity is not meaningful)')
    db = corpus.connect()
    if args.document:
        try:
            total, rows = find_documents(db, args.phrases, args.limit)
        finally:
            db.close()
        print(f'{total:,} matching documents (showing {len(rows)}, document ID order)\n')
        official = document_urls({row[0] for row in rows})
        for efta, dataset, doc_type, url, evidence in rows:
            url = url or official.get(efta)
            print(f'{efta}  dataset={dataset} type={doc_type}' + (f'\n  {url}' if url else ''))
            for text, page, snippet in evidence:
                print(f'  {text!r} p.{page}: ' + ' '.join(snippet.split()))
            print()
        return
    query = scope(build_query(args.phrases, args.near))
    rows = db.execute('''SELECT p.doc_id, p.page, json_extract(d.meta, '$.dataset'), d.type, d.url,
                                snippet(pages_fts, 2, '[', ']', ' … ', 24)
                         FROM main.pages_fts JOIN pages p ON p.page_id = pages_fts.rowid
                         LEFT JOIN documents d ON d.id = p.doc_id
                         WHERE pages_fts MATCH ? ORDER BY rank LIMIT ?''', (query, args.limit)).fetchall()
    total = db.execute('SELECT count(*) FROM main.pages_fts WHERE pages_fts MATCH ?', (query,)).fetchone()[0]
    db.close()
    print(f'{total:,} matching pages for {query} (showing {len(rows)}, best FTS rank first)\n')
    official = document_urls({row[0] for row in rows})
    for efta, page, dataset, doc_type, url, snippet in rows:
        url = url or official.get(efta)
        print(f'{efta} p.{page}  dataset={dataset} type={doc_type}' + (f'\n  {url}' if url else ''))
        print('  ' + ' '.join(snippet.split()) + '\n')


if __name__ == '__main__':
    main()
