"""Per-document facts from the upstream v5.2 analysis databases, and subpoena targets.

    .venv/bin/python scripts/build_doc_info.py

Writes output/document_info.db (public: no names beyond subpoena targets, which DOJ
published):
  info(efta, doc_type, doc_type_confidence, release_status, removal_kind, sensitivity,
       redactions, bad_redactions, native_type, fbi_case, thread_root, reports, line)
    doc_type        page_classifications.document_types (classifier vote over pages)
    release_status  document_status_clean: altered / removed / unchanged between DOJ versions
    redactions      redaction_analysis_v2.document_summary (counts only)
    fbi_case        secondary_stamps.fbi_case_serials case label ("SDNY Trafficking")
    reports         report_mentions: titles of published analyses citing the document
    line            short text the viewer shows beside a document link (redaction counts are
                    left out of it: they point at recoverable redactions)
  subpoenas(efta, target, category, date_issued, statutes, clauses)
Text recovered from under redactions (reconstructed_pages) and text removed between
DOJ versions (alteration_results.diff_details) are deliberately not used: removed
text is mostly names withheld to protect survivors.
"""
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / 'data/raw'
OUT = ROOT / 'output/document_info.db'
MIN_TYPE_CONFIDENCE = 0.35
SKIP_TYPES = {'misc', 'blank', 'email', 'plist'}  # too common, empty or opaque to be worth a label


def rows(name, query):
    path = RAW / name
    if not path.exists():
        return []
    with closing(sqlite3.connect(f'file:{path}?mode=ro&immutable=1', uri=True)) as db:
        return db.execute(query).fetchall()


def type_label(doc_type):
    """'financial.bank_statement' -> 'bank statement', 'court.filing' -> 'court filing', 'memo' -> 'memo'."""
    group, _, kind = doc_type.partition('.')
    if not kind:
        return group.replace('_', ' ')
    return kind.replace('_', ' ') if group in ('financial', 'travel', 'research', 'image') else f"{group} {kind.replace('_', ' ')}"


def main():
    info = defaultdict(dict)
    for efta, doc_type, confidence in rows('page_classifications.db', 'SELECT efta_number, doc_type, confidence FROM document_types'):
        info[efta].update(doc_type=doc_type, doc_type_confidence=round(confidence, 3))
    for efta, status, kind, sensitivity in rows('document_status_clean.db',
                                                'SELECT efta_number, status, classification, sensitivity FROM document_status'):
        info[efta].update(release_status=status, removal_kind=(kind or '').lower() or None, sensitivity=(sensitivity or '').lower() or None)
    for efta, total, bad in rows('redaction_analysis_v2.db',
                                 'SELECT efta_number, sum(total_redactions), sum(bad_redactions) FROM document_summary GROUP BY 1'):
        if efta and total:
            info[efta].update(redactions=total, bad_redactions=bad or 0)
    for efta, extension, category in rows('native_files.db', 'SELECT efta_number, extension, category FROM native_files'):
        info[efta]['native_type'] = ' '.join(x for x in (category, extension and f'.{extension}') if x) or None
    for efta, label in rows('secondary_stamps.db', 'SELECT efta_number, min(case_label) FROM fbi_case_serials GROUP BY 1'):
        info[efta]['fbi_case'] = label
    reports = defaultdict(set)
    for efta, title in rows('report_mentions.db', 'SELECT doc_id, report_title FROM report_mentions'):
        reports[efta].add(title)
    for efta, titles in reports.items():
        info[efta]['reports'] = json.dumps(sorted(titles), ensure_ascii=False)
    for root, efta in rows('concordance_complete.db', '''SELECT r.bates_begin, d.bates_begin FROM email_threads t
            JOIN documents r ON r.id = t.thread_root_id JOIN documents d ON d.id = t.doc_id WHERE t.thread_root_id != t.doc_id'''):
        info[efta]['thread_root'] = root
    subpoenas = rows('prosecutorial_query_graph.db', '''SELECT efta_number, target, target_category, date_issued, statutes, clause_count
            FROM subpoenas WHERE target IS NOT NULL AND target NOT LIKE '%REDACTED%' ''')
    for efta, target, *_ in subpoenas:
        info[efta]['subpoena_target'] = target

    def line(item):
        parts = []
        doc_type = item.get('doc_type') or ''
        if doc_type and doc_type.split('.')[0] not in SKIP_TYPES and item.get('doc_type_confidence', 0) >= MIN_TYPE_CONFIDENCE:
            parts.append(type_label(doc_type))
        if item.get('subpoena_target'):
            parts.append(f"subpoena to {item['subpoena_target']}")
        if item.get('fbi_case'):
            parts.append(f"FBI {item['fbi_case']}")
        if item.get('release_status') in ('altered', 'removed'):
            parts.append(f"{item['release_status']} in a later DOJ version")
        if item.get('reports'):
            n = len(json.loads(item['reports']))
            parts.append(f'cited in {n} published report{"s" * (n != 1)}')
        if item.get('native_type'):
            parts.append(item['native_type'])
        return ' · '.join(parts) or None

    columns = ('doc_type', 'doc_type_confidence', 'release_status', 'removal_kind', 'sensitivity', 'redactions', 'bad_redactions',
               'native_type', 'fbi_case', 'thread_root', 'reports', 'subpoena_target')
    partial = OUT.with_suffix('.partial.db')
    partial.unlink(missing_ok=True)
    with closing(sqlite3.connect(partial)) as db:
        db.execute(f'CREATE TABLE info(efta TEXT PRIMARY KEY, {", ".join(columns)}, line TEXT)')
        db.executemany(f'INSERT INTO info VALUES ({", ".join("?" * (len(columns) + 2))})',
                       [(efta, *(item.get(c) for c in columns), line(item)) for efta, item in sorted(info.items())])
        db.execute('CREATE TABLE subpoenas(efta TEXT, target TEXT, category TEXT, date_issued TEXT, statutes TEXT, clauses INTEGER)')
        db.executemany('INSERT INTO subpoenas VALUES (?,?,?,?,?,?)', subpoenas)
        db.commit()
        summary = {'documents': len(info), 'with_line': db.execute('SELECT count(*) FROM info WHERE line IS NOT NULL').fetchone()[0],
                   **{c: db.execute(f'SELECT count(*) FROM info WHERE {c} IS NOT NULL').fetchone()[0] for c in columns},
                   'subpoenas': len(subpoenas)}
    partial.replace(OUT)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
