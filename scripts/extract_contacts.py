"""Email addresses and phone numbers written anywhere in the full-text corpus
(and the extra pages of build_supplement.py: text read off images, handwriting).

Header extraction (build_communications.py) only reads From/To/Cc fields of
email records. Contact values also appear in body text: contact lists and the
address book, phone-message pads, signatures, forwarded contact cards, letters.
This scans every page with patterns and records each value with its document,
page, a context label (tel/fax/cell/... or email/mailto) and whether the line is
an email header line (already covered by the header data), plus the name
written next to it (same line just before it, or for phones a name-only line
in the 3 lines above: signature blocks).

Phones: NANP numbers need separators ("212-555-1234", "(212) 555 1234",
"212.555.1234", optional +1) so bare digit runs (account and Bates numbers) are
not read as phones; international numbers need a leading "+". Stored as E.164
like the contact lookup ("+12125551234"). Emails: normalized like
build_contacts.normalize_contact (domain case-folded).

PRIVATE: raw contact values. Output: data/private/fulltext_contacts.db
(mentions(kind, value, efta, page, label, header, name, name_how)); summary with counts only
in output/fulltext_contacts_summary.json.

Usage: extract_contacts.py [--limit PAGES] [--workers N]
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing
from multiprocessing import get_context
from pathlib import Path

from build_contacts import normalize_contact, placeholder_number
import corpus as corpus_source
from supplement import supplement_pages

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data/private/fulltext_contacts.db'
SUMMARY = ROOT / 'output/fulltext_contacts_summary.json'
BATCH = 20000  # pages per worker task

EMAIL = re.compile(r"(?<![\w.%+'-])([A-Za-z0-9][A-Za-z0-9._%+'-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,12})(?![\w-])")
NANP = re.compile(r'(?<![\w.+/-])(?:\+?1[\s.-]?)?(\(\s?[2-9]\d{2}\s?\)|[2-9]\d{2})([\s.-]{0,2})([2-9]\d{2})[\s.-]([0-9]{4})(?![\w/]|[\s.-]?\d)')
INTL = re.compile(r'(?<![\w+])\+([2-9]\d{0,2})[\s.-]?(\(?0?\)?[\s.-]?\d{1,4})((?:[\s.-]?\d{2,4}){2,4})(?![\w/]|[\s.-]?\d)')
LABEL = re.compile(r'(?i)\b(tel(?:ephone)?|phone|ph|cell(?:ular)?|mobile|mob|fax|facsimile|home|office|direct|work|main|switchboard|'
                   r'[tfmhwcop])\b\s*(?:\(\w\))?\s*[:.#-]?\s*$')
HEADER_LINE = re.compile(r'(?im)^[ \t>]*(?:from|to|cc|bcc|sent|reply-to|sender|date|subject)\s*:')
MAILTO = re.compile(r'(?i)(?:mailto|malito|mallto|maiito)[:;]?$')
# Image/attachment IDs and template placeholders are not mailboxes.
NOT_MAILBOX = re.compile(r'(?i)^(?:image\d+\.\w+|[0-9a-f]{8,}|.*\.(?:png|jpe?g|gif))@|@(?:example\.|domain\.|email\.com$|xx+\.)')


NAME_WORD = r"(?:[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)*|[A-Z][a-z]?[A-Z][a-z]+|[A-Z]\.?)"
NAME_JOIN = r"(?:de|van|von|da|del|della|di|du|la|le|bin|al)"
NAME_AT_END = re.compile(rf'({NAME_WORD}(?:[ \t]+(?:{NAME_WORD}|{NAME_JOIN})){{1,4}})$')
NAME_LINE = re.compile(rf'^[ \t]*({NAME_WORD}(?:[ \t]+(?:{NAME_WORD}|{NAME_JOIN})){{1,4}})[ \t,]*$')
NOT_NAME = {'tel', 'telephone', 'phone', 'ph', 'fax', 'facsimile', 'cell', 'mobile', 'mob', 'email', 'mail', 'mailto', 'office',
            'direct', 'main', 'home', 'work', 'contact', 'from', 'to', 'cc', 'bcc', 'sent', 'date', 'subject', 're', 'fw', 'fwd',
            'dear', 'hi', 'hello', 'thanks', 'thank', 'regards', 'best', 'call', 'at', 'or', 'and', 'the', 'number', 'no', 'cellular',
            'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov', 'dec', 'january', 'february',
            'march', 'april', 'june', 'july', 'august', 'september', 'october', 'november', 'december', 'monday', 'tuesday',
            'wednesday', 'thursday', 'friday', 'saturday', 'sunday', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun', 'am', 'pm',
            'on', 'wrote', 'sender', 'reply', 'user', 'name', 'names', 'last', 'first', 'full', 'login', 'username', 'confidential', 'privileged', 'note', 'new', 'york', 'suite', 'floor', 'street',
            'avenue', 'road', 'usa', 'us', 'inc', 'llc', 'ltd', 'corp', 'p', 'c', 'm', 't', 'f', 'h', 'w', 'o', 'e'}
# Job titles and organisation words: a line made of them is not a person's name.
NOT_PERSON = {'director', 'managing', 'president', 'partner', 'partners', 'associate', 'associates', 'assistant', 'manager',
              'vice', 'chairman', 'chair', 'ceo', 'cfo', 'coo', 'executive', 'senior', 'counsel', 'attorney', 'esq', 'secretary',
              'officer', 'analyst', 'principal', 'founder', 'chief', 'head', 'department', 'group', 'services', 'service',
              'capital', 'bank', 'banking', 'management', 'law', 'firm', 'offices', 'university', 'foundation', 'company',
              'international', 'global', 'private', 'wealth', 'client', 'clients', 'team', 'desk', 'support', 'customer',
              'center', 'centre', 'hotel', 'airport', 'aviation', 'travel', 'securities', 'trust', 'fund', 'holdings',
              'general', 'legal', 'investment', 'investments', 'advisor', 'adviser', 'agent', 'reception', 'operator',
              'administrator', 'coordinator', 'specialist', 'consultant', 'representative', 'sales', 'marketing', 'media',
              'press', 'news', 'editor', 'producer', 'school', 'college', 'institute', 'hospital', 'clinic', 'medical',
              'police', 'department', 'court', 'district', 'county', 'state', 'federal', 'united', 'states', 'america',
              'island', 'islands', 'beach', 'palm', 'virgin', 'saint', 'st', 'avenue', 'boulevard', 'plaza', 'tower', 'building',
              'chase', 'pmorgan', 'jpmorgan', 'morgan', 'hearing', 'impaired', 'bureau', 'credit', 'card', 'portfolio',
              'activity', 'assigned', 'cellphone', 'account', 'statement', 'distinguished', 'investigator', 'professor',
              'protection', 'financial', 'consumer', 'toll', 'free', 'hotline', 'line', 'dept', 'division', 'unit'}
PREFIX_JUNK = re.compile(r'[\s<\[("\'“:;,|/-]*(?:mailto:?)?[\s<\[("\'“:;,|/-]*$', re.I)


def clean_name(candidate):
    """Strip label/date words at the edges; a name needs two real words."""
    words = candidate.replace('’', "'").split()
    while words and words[0].strip('.').casefold() in NOT_NAME:
        words.pop(0)
    while words and words[-1].strip('.').casefold() in NOT_NAME:
        words.pop()
    if sum(len(w.strip('.')) > 1 for w in words) < 2 or any(w.strip('.').casefold() in NOT_NAME | NOT_PERSON for w in words):
        return None
    return ' '.join(words)


def name_near(text, start, kind):
    """(name, how) written next to a contact value: on its line just before it
    ("Jane Roe <jane@x>", "Jane Roe, Cell: 917-..."), or for phones the nearest
    name-only line within the 3 lines above (signature blocks). None if none."""
    line_start = text.rfind('\n', 0, start) + 1
    prefix = text[line_start:start]
    if kind == 'phone':
        prefix = LABEL.sub('', prefix)
    prefix = PREFIX_JUNK.sub('', prefix)
    found = NAME_AT_END.search(prefix)
    if found and (name := clean_name(found.group(1))):
        return name, 'line'
    if kind == 'phone':
        above = text[max(0, line_start - 300):max(0, line_start - 1)].split('\n')[-3:]
        for line in reversed(above):
            found = NAME_LINE.match(line)
            if found and (name := clean_name(found.group(1))):
                return name, 'block'
    return None, ''


def phone_label(text, start):
    found = LABEL.search(text[max(0, start - 24):start])
    if not found:
        return ''
    word = found.group(1).casefold()
    return {'t': 'tel', 'telephone': 'tel', 'ph': 'phone', 'f': 'fax', 'facsimile': 'fax', 'm': 'cell', 'c': 'cell',
            'mob': 'cell', 'mobile': 'cell', 'cellular': 'cell', 'h': 'home', 'w': 'work', 'o': 'office', 'p': 'phone'}.get(word, word)


def header_spans(text):
    """[(start, end)] of email header lines (From:/To:/Cc:/... up to the line end)."""
    return [(m.start(), text.find('\n', m.end()) % (len(text) + 1)) for m in HEADER_LINE.finditer(text)]


def in_spans(spans, at):
    return any(a <= at < b for a, b in spans)


# Keep source offsets for header/name evidence. Repair only address-shaped
# spans, never join arbitrary adjacent prose words into an invented mailbox.
OCR_EMAIL = re.compile(
    r"(?<![\w.%+'-])([A-Za-z0-9][A-Za-z0-9_%+'-]*(?:[ \t]*\.[ \t\r\n]*[A-Za-z0-9_%+'-]+)*"
    r"[ \t\r\n]*(?:@|©|®|\(a\))[ \t\r\n]*"
    r"[A-Za-z0-9-]+(?:[ \t\r\n]*\.[ \t\r\n]*[A-Za-z0-9-]+)+)(?![\w-])", re.I)


def phone_extension(text, end):
    match = re.match(r'[ \t]*(?:ext(?:ension)?\.?|x|#)[ \t]*(\d{1,8})\b', text[end:], re.I)
    return 'x' + match[1] if match else ''


def extract(text):
    """[(kind, value, label, header, name, how)] in one page's text."""
    if not text:
        return []
    out, spans = [], None
    email_matches = list(EMAIL.finditer(text)) if '@' in text else []
    covered = [(m.start(),m.end()) for m in email_matches]
    email_matches += [m for m in OCR_EMAIL.finditer(text) if not any(a <= m.start() and m.end() <= b for a,b in covered)]
    for m in email_matches:
        raw = re.sub(r"\s+", "", m.group(1)).replace("©", "@").replace("®", "@").replace("(a)", "@").replace("(A)", "@").strip(".'-")
        value = normalize_contact(raw, 'email')
        if not value or NOT_MAILBOX.search(value):
            continue
        spans = header_spans(text) if spans is None else spans
        label = 'mailto' if MAILTO.search(text[max(0, m.start() - 8):m.start()]) else 'email'
        out.append(('email', value, label, in_spans(spans, m.start()), *name_near(text, m.start(), 'email')))
    for m in NANP.finditer(text):
        digits = re.sub(r'\D', '', m.group(1)) + m.group(3) + m.group(4)
        if placeholder_number(digits) or digits[3:6] == '555' and digits[6:8] == '01':
            continue  # 555-01xx: fictional
        spans = header_spans(text) if spans is None else spans
        out.append(('phone', '+1' + digits + phone_extension(text, m.end()), phone_label(text, m.start()), in_spans(spans, m.start()), *name_near(text, m.start(), 'phone')))
    for m in (INTL.finditer(text) if '+' in text else ()):
        country = m.group(1)
        digits = country + re.sub(r'\D', '', m.group(2)).lstrip('0') + re.sub(r'\D', '', m.group(3))
        if not 8 <= len(digits) <= 15 or placeholder_number(digits):
            continue
        spans = header_spans(text) if spans is None else spans
        out.append(('phone', '+' + digits + phone_extension(text, m.end()), phone_label(text, m.start()), in_spans(spans, m.start()), *name_near(text, m.start(), 'phone')))
    return out


_corpus = None


def mentions_identical(partial, target):
    """Same schema and mention multiset, despite worker completion order.

    An unordered pool can lay out identical rows differently in SQLite. Keep
    the previous bytes in that case so downstream indexes/pages stay current.
    Group counts include duplicates and SQL NULLs; EXCEPT alone would not.
    """
    if not target.exists():
        return False
    # ATTACH reads are invisible to the build's sqlite3.connect audit hook.
    with target.open('rb'):
        pass
    try:
        with closing(sqlite3.connect(f'file:{partial}?mode=ro', uri=True)) as db:
            db.execute('ATTACH DATABASE ? AS previous', (f'file:{target}?mode=ro',))
            schema = 'SELECT type,name,sql FROM {}.sqlite_master ORDER BY type,name'
            if db.execute(schema.format('main')).fetchall() != db.execute(schema.format('previous')).fetchall():
                return False
            if db.execute('SELECT count(*) FROM mentions').fetchone() != db.execute('SELECT count(*) FROM previous.mentions').fetchone():
                return False
            columns = 'kind,value,efta,page,label,header,name,name_how'
            return db.execute(f'SELECT {columns},count(*) FROM mentions GROUP BY {columns} '
                              f'EXCEPT SELECT {columns},count(*) FROM previous.mentions GROUP BY {columns} LIMIT 1').fetchone() is None
    except sqlite3.DatabaseError:
        return False  # replace an unreadable old artifact with the valid new one


def scan(bounds):
    """Mentions in pages with lo <= id < hi (runs in a worker)."""
    global _corpus
    if _corpus is None:
        _corpus = corpus_source.connect()
    lo, hi = bounds
    rows = []
    pages = _corpus.execute('SELECT doc_id, page, text FROM pages WHERE page_id >= ? AND page_id < ?', (lo, hi)) \
        if lo is not None else [(efta, page, text) for _, efta, page, text in supplement_pages()]  # build_supplement.py
    for efta, page, text in pages:
        for kind, value, label, header, name, how in set(extract(text)):
            rows.append((kind, value, efta, page, label, int(header), name, how))
    return (hi - lo) if lo is not None else 0, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--limit', type=int, help='scan only the first N page ids (pilot)')
    parser.add_argument('--workers', type=int, default=3)
    args = parser.parse_args()
    started = time.time()
    with closing(corpus_source.connect()) as corpus:
        lo, hi = corpus.execute('SELECT min(page_id), max(page_id) + 1 FROM pages').fetchone()
    if args.limit:
        hi = min(hi, lo + args.limit)
    partial = OUT.with_suffix('.partial.db')
    partial.unlink(missing_ok=True)
    old_mask = os.umask(0o077)  # raw contact values: owner-only
    try:
        db = sqlite3.connect(partial)
    finally:
        os.umask(old_mask)
    db.execute('CREATE TABLE mentions(kind TEXT, value TEXT, efta TEXT, page INTEGER, label TEXT, header INTEGER, name TEXT, name_how TEXT)')
    done = 0
    # Explicit fork avoids Python 3.14 forkserver sockets and keeps worker
    # startup compatible with restricted/headless build environments.
    with get_context("fork").Pool(args.workers) as pool:
        supplement = [(None, None)] if supplement_pages() and not args.limit else []  # text read off images, handwriting
        for n, rows in pool.imap_unordered(scan, [(a, min(a + BATCH, hi)) for a in range(lo, hi, BATCH)] + supplement):
            db.executemany('INSERT INTO mentions VALUES (?,?,?,?,?,?,?,?)', rows)
            db.commit()
            done += n
            print(f'\r{done:,}/{hi - lo:,} page ids, {time.time() - started:.0f}s', end='', flush=True)
    print()
    db.executescript('CREATE INDEX mentions_value ON mentions(kind, value); CREATE INDEX mentions_efta ON mentions(efta);')
    summary = {'pages_scanned': hi - lo, 'limit': args.limit, 'seconds': round(time.time() - started)}
    for kind, values, docs, mentions, body in db.execute(
            'SELECT kind, count(DISTINCT value), count(DISTINCT efta), count(*), sum(header = 0) FROM mentions GROUP BY kind'):
        summary[kind] = {'distinct_values': values, 'documents': docs, 'page_mentions': mentions, 'outside_header_lines': body}
    summary['phone_labels'] = dict(Counter(dict(db.execute("SELECT label, count(*) FROM mentions WHERE kind='phone' GROUP BY label"))).most_common())
    db.close()
    if mentions_identical(partial, OUT):
        partial.unlink()
        sys.audit('epstein.output', str(OUT))
    else:
        partial.replace(OUT)
    SUMMARY.write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
