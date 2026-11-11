"""External email adapter: published dumps outside the DOJ release, headers only
(bodies ignored), used as published with redactions intact.

  data/external/emails/<source>/**/*.eml|*.mbox
  data/external/jmail/emails-slim.ndjson.gz   Jmail export, filtered by JMAIL_DROPS,
                                              promotional mail skipped
"""
import datetime
import gzip
import json
import mailbox
import re
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / 'data/external'
REDACTION_BLOCK = re.compile(r'<?[█■▓]+>?')
# Jmail drops imported: the leaked Yahoo account only. DOJ volumes and House
# Oversight emails ('original') are already in email_metadata.db; Ehud Barak's
# mailbox ('ehud_ddos_dropsite_1') was obtained by hacking and is excluded.
JMAIL_DROPS = {'yahoo_2': 'jmail_yahoo_leak'}


def unredacted_part(value):
    """Drop redaction blocks so a visible name still links ("Name <████>" -> "Name").
    The hidden text itself is never recovered."""
    return ' '.join(REDACTION_BLOCK.sub(' ', value).split()) if isinstance(value, str) else value


def json_list(value):
    try:
        items = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return [value]
    return items if isinstance(items, list) else []


def header_date(value):
    try:
        return parsedate_to_datetime(value).date().isoformat() if value else None
    except (TypeError, ValueError, IndexError):
        return None


def iso_day(value):
    """'YYYY-MM-DD' if value is a valid date in that form, else None."""
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return None


def messages(root=None):
    """Yield (source, relative_path, index, message_id, sender, to_list, cc_list, date YYYY-MM-DD or None)."""
    root = root or PATH
    emails = root / 'emails'
    parser = BytesHeaderParser()
    for source_dir in sorted(p for p in emails.iterdir() if p.is_dir()) if emails.exists() else []:
        index = 0
        for path in sorted(source_dir.rglob('*')):
            box = None
            if path.suffix.lower() == '.eml':
                with path.open('rb') as f:
                    found = [parser.parse(f)]
            elif path.suffix.lower() == '.mbox':
                found = box = mailbox.mbox(path, create=False)
            else:
                continue
            try:
                for message in found:
                    index += 1
                    yield (source_dir.name, str(path.relative_to(source_dir)), index,
                           (message.get('Message-ID') or '').strip(), str(message.get('From') or ''),
                           [str(v) for v in message.get_all('To') or []], [str(v) for v in message.get_all('Cc') or []],
                           header_date(message.get('Date')))
            finally:
                if box is not None:
                    box.close()
    jmail = root / 'jmail/emails-slim.ndjson.gz'
    if jmail.exists():
        with gzip.open(jmail, 'rt') as f:
            for index, line in enumerate(f, 1):
                r = json.loads(line)
                source = JMAIL_DROPS.get(r.get('email_drop_id'))
                if source is None or str(r.get('is_promotional')) == 'True':
                    continue
                yield (source, 'jmail:' + str(r.get('doc_id')), index, 'jmail:' + str(r.get('id')),
                       unredacted_part(r.get('sender') or ''),
                       [unredacted_part(v) for v in json_list(r.get('to_recipients'))],
                       [unredacted_part(v) for v in json_list(r.get('cc_recipients'))], iso_day((r.get('sent_at') or '')[:10]))


def stamp_paths(root=None):
    return sorted((root or PATH).rglob('*.ndjson.gz'))
