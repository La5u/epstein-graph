"""EFTA email header adapter: upstream (rhowardstone) header tables parsed from the
DOJ release. Reads the files in place; TEMP views give their tables the common
column names (see scripts/emails.py), nothing is copied.

  data/raw/email_metadata.db  emails_merged: one row per message header (name + address,
                              recipients as JSON arrays), the main header source
  data/raw/communications.db  emails / email_participants / resolved_identities: per-address
                              participants with upstream identity resolution
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEADERS_PATH = ROOT / 'data/raw/email_metadata.db'
PARTIES_PATH = ROOT / 'data/raw/communications.db'
# Provenance written into evidence rows: (source file, table).
HEADERS_SOURCE = ('email_metadata.db', 'emails_merged')
PARTIES_SOURCE = ('communications.db', 'emails')


def _connect(path):
    return sqlite3.connect(f'file:{path}?mode=ro&immutable=1', uri=True)


def headers(path=None):
    """Connection with TEMP view headers over emails_merged."""
    db = _connect(path or HEADERS_PATH)
    db.execute('''CREATE TEMP VIEW headers AS SELECT id, efta_number AS doc_id, message_index, sender AS sender_name,
        sender_email, to_recipients AS to_json, cc_recipients AS cc_json, date_sent AS date, is_noise AS noise
        FROM main.emails_merged''')
    return db


def parties(path=None):
    """Connection with TEMP views messages, parties and identities."""
    db = _connect(path or PARTIES_PATH)
    db.execute('''CREATE TEMP VIEW messages AS SELECT id, efta_number AS doc_id, date_normalized AS date,
        is_noise AS noise FROM main.emails''')
    db.execute('''CREATE TEMP VIEW parties AS SELECT email_id AS message_id, role, name, email_address AS address,
        resolved_entity_id AS identity_id FROM main.email_participants''')
    db.execute('''CREATE TEMP VIEW identities AS SELECT id, canonical_name AS name, confidence,
        match_method AS method FROM main.resolved_identities''')
    return db


def create(headers_path=None, parties_path=None, headers=(), messages=(), parties=(), identities=()):
    """Test fixtures in the source layout. headers: (id, doc_id, message_index, sender, sender_email, to JSON,
    cc JSON, date, noise); messages: (id, doc_id, date, noise); parties: (message_id, role, name, address,
    identity_id); identities: (id, canonical_name, confidence, method)."""
    if headers_path:
        with sqlite3.connect(headers_path) as db:
            db.execute('''CREATE TABLE emails_merged (id INTEGER PRIMARY KEY, efta_number TEXT, message_index INTEGER,
                sender TEXT, sender_email TEXT, to_recipients TEXT, cc_recipients TEXT, date_sent TEXT, is_noise INTEGER)''')
            db.executemany('INSERT INTO emails_merged VALUES (?,?,?,?,?,?,?,?,?)', headers)
        db.close()
    if parties_path:
        with sqlite3.connect(parties_path) as db:
            db.execute('CREATE TABLE emails (id INTEGER PRIMARY KEY, efta_number TEXT, date_normalized TEXT, is_noise INTEGER)')
            db.execute('''CREATE TABLE email_participants (id INTEGER PRIMARY KEY, email_id INTEGER, role TEXT, name TEXT,
                email_address TEXT, resolved_entity_id INTEGER)''')
            db.execute('''CREATE TABLE resolved_identities (id INTEGER PRIMARY KEY, canonical_name TEXT, confidence REAL,
                match_method TEXT)''')
            db.executemany('INSERT INTO emails VALUES (?,?,?,?)', messages)
            db.executemany('INSERT INTO email_participants (email_id, role, name, email_address, resolved_entity_id) '
                           'VALUES (?,?,?,?,?)', parties)
            db.executemany('INSERT INTO resolved_identities VALUES (?,?,?,?)', identities)
        db.close()
