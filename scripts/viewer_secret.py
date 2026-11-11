"""Encrypted real names for the single viewer (output/graph.html).

scripts/private_contacts.py writes the plaintext reveal table to
data/private/viewer_secret.json (0600). visualize_graph.py encrypts the part
the viewer needs with the password in data/private/viewer_password and embeds
it; the page shows pseudonyms until the password is entered, then decrypts in
the browser (WebCrypto). Encryption: PBKDF2-SHA256 (600k iterations) ->
AES-256-GCM. Anyone holding the HTML can try passwords offline, so use a long
one and only share the file if that is acceptable. Following the GDPR review
of 2026-11-10, public contacts are limited to attested organisation contacts
and (config/public_contacts.json, default ON) contacts of people already named
by the public-names decisions. All others use stable HMAC-SHA256 labels with
12 hex characters; the separate 0600 key in data/private is never embedded.
Every real value remains available inside the password-encrypted SECRET.
Viewer plaintext is gzip-compressed before encryption (compression='gzip');
the browser also accepts older blobs without compression.

Set or change the password (prompts; nothing is echoed or logged):
    .venv/bin/python scripts/viewer_secret.py --set-password
"""
import base64
import getpass
import gzip
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / 'data/private'
SECRET = PRIVATE / 'viewer_secret.json'
PASSWORD = PRIVATE / 'viewer_password'
ITERATIONS = 600_000
MIN_LENGTH = 12


def _write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    path.chmod(0o600)


def write_secret(reveal, doc_pages, reasons=None):
    """reveal: {contact_id: {'value', 'possible_duplicates'}}; doc_pages: {node_id: 'node_docs/….html'};
    reasons: {member id: reviewed merge / split reason} (config texts quote real names)."""
    _write_private(SECRET, json.dumps({'reveal': reveal, 'pages': {str(k): v for k, v in doc_pages.items()}, 'reasons': reasons or {}},
                                      ensure_ascii=False))


def encrypt(plaintext, password, iterations=None, salt=None, iv=None):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    iterations, salt, iv = iterations or ITERATIONS, salt or os.urandom(16), iv or os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(password.encode())
    data = AESGCM(key).encrypt(iv, plaintext, None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {'v': 1, 'iterations': iterations, 'salt': b64(salt), 'iv': b64(iv), 'data': b64(data)}


def reason_wanted(key, wanted):
    """Whether a reason key (plain member ID, 'attr:<bare id>:<owner id>' or
    'apart:<id>|<id>') only names nodes of this viewer (wanted: set of str IDs)."""
    if key.startswith('attr:'):
        parts = key.split(':')[1:]
    else:
        parts = key[len('apart:'):].split('|') if key.startswith('apart:') else [key]
    return all(part in wanted for part in parts)


PUBLIC_KINDS = ('email_', 'phone_')


def viewer_ids(entities):
    """Every node and member ID the viewer can show a value for."""
    ids = set()
    for e in entities:
        ids.add(e['id'])
        ids.update(m['id'] for m in e.get('merged_identities', []) if 'id' in m)
        ids.update(x['id'] for x in e.get('expand', []))
        for items in e.get('identity_review', {}).values():
            ids.update(x for m in items for x in (m.get('id'), m.get('canonical'), m.get('member')) if x is not None)
    return ids


def public_reveal(entities):
    """Safe contacts and HMAC labels; same publication policy as the viewer."""
    from contact_publication import publish, named_public_ids
    import public_names
    from viewer_label_payload import secret_entries
    wanted = set(map(str, viewer_ids(entities)))
    entries = {i: value for table,i,value in secret_entries(SECRET)
               if table == 'reveal' and i in wanted} if SECRET.is_file() else {}
    values = {i:value['value'] for i,value in entries.items()}
    kinds = {i:value.get('kind') for i,value in entries.items()}
    verdicts = public_names.load_verdicts()
    import survivor_privacy
    public, _, _, scrub, denied = survivor_privacy.protect(entities, [], survivor_privacy.load_key())
    public, _, _ = public_names.apply(public, verdicts, survivor_privacy.load_key(), survivor_privacy.scrubber)
    allowed = survivor_privacy.public_contacts(values, scrub, denied, survivor_privacy.survivor_mailbox_forms())
    return publish(public, values, named_ids=named_public_ids(public, values, verdicts),
                   denied=set(values)-set(allowed), kinds=kinds)[0]



def viewer_secret(entities, html_path):
    """Encrypted {reveal, pages} for the entities in this viewer, or None when
    there is no reveal table or no password (the page then has no unlock box)."""
    if not (SECRET.is_file() and PASSWORD.is_file()):
        return None
    password = PASSWORD.read_text().strip()
    if len(password) < MIN_LENGTH:
        raise SystemExit(f'{PASSWORD} is shorter than {MIN_LENGTH} characters; set a longer one with --set-password')
    table = json.loads(SECRET.read_text())
    reveal, pages = table['reveal'], table['pages']
    ids = viewer_ids(entities)
    base = os.path.relpath(PRIVATE, Path(html_path).resolve().parent)  # node_docs links relative to the HTML
    reasons = table.get('reasons', {})  # reviewed merge reasons quote real names
    wanted = set(map(str, ids))
    keep = lambda k: reason_wanted(k, wanted)
    payload = {'reveal': {i: reveal[i] for i in ids if i in reveal}, 'reasons': {k: v for k, v in reasons.items() if keep(k)},
               'pages': {str(e['id']): f"{base}/{pages[str(e['id'])]}" for e in entities if str(e['id']) in pages}}
    encrypted = encrypt(gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0), password)
    return {**encrypted, 'compression': 'gzip'}


def set_password():
    first = getpass.getpass(f'Viewer password (at least {MIN_LENGTH} characters): ')
    if len(first) < MIN_LENGTH:
        raise SystemExit('Too short; nothing changed.')
    if getpass.getpass('Repeat: ') != first:
        raise SystemExit('Passwords differ; nothing changed.')
    _write_private(PASSWORD, first)
    print(f'Saved to {PASSWORD} (mode 0600). Rebuild the viewer to re-encrypt: .venv/bin/python scripts/visualize_graph.py --no-rebuild')


if __name__ == '__main__':
    if '--set-password' in sys.argv[1:]:
        set_password()
    else:
        print(__doc__)
