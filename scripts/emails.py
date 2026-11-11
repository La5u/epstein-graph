"""The common email header format pipeline steps read; sources plug in as adapters
(scripts/adapters/). Steps never open a source file or use its column names.

headers() gives a read-only connection with the view
  headers(id, doc_id, message_index, sender_name, sender_email, to_json, cc_json, date, noise)
    one row per parsed message header; to_json / cc_json: JSON arrays of recipient strings
    (or [name, address] pairs); date YYYY-MM-DD as given (unvalidated); noise 1 = not a message
parties() gives a read-only connection with the views
  messages(id, doc_id, date, noise)
  parties(message_id, role, name, address, identity_id)     role: from / to / cc / bcc
  identities(id, name, confidence, method)                  upstream resolution; method: exact,
                                                            email, fuzzy, manual
external() yields messages from published dumps outside the corpus:
  (source, relative_path, index, message_id, sender, to_list, cc_list, date)
HEADERS_SOURCE / PARTIES_SOURCE: (file, table) provenance for evidence rows.

Adapters: adapters/efta_emails.py (headers, parties), adapters/external_emails.py.
"""
from adapters import efta_emails, external_emails

HEADERS_SOURCE, PARTIES_SOURCE = efta_emails.HEADERS_SOURCE, efta_emails.PARTIES_SOURCE
headers, parties = efta_emails.headers, efta_emails.parties
external = external_emails.messages


def stamp_paths():
    """Source files whose size and mtime key caches derived from the headers."""
    return [efta_emails.HEADERS_PATH, efta_emails.PARTIES_PATH, *external_emails.stamp_paths()]


def use_root(root):
    """Patch context pointing every adapter at root/data (tests)."""
    from contextlib import ExitStack
    from unittest.mock import patch
    stack = ExitStack()
    stack.enter_context(patch.object(efta_emails, 'HEADERS_PATH', root / 'data/raw/email_metadata.db'))
    stack.enter_context(patch.object(efta_emails, 'PARTIES_PATH', root / 'data/raw/communications.db'))
    stack.enter_context(patch.object(external_emails, 'PATH', root / 'data/external'))
    return stack
