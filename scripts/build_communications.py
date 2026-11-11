"""Stream merged email metadata into a private-contact-safe SQLite graph.

Counts are extracted message records, NOT deduplicated real-world emails.
No subjects, raw header strings, contact values or reverse maps are exported.
The browser receives a bounded slice; SQLite retains the complete derived graph.
"""
import argparse
import bisect
import datetime
import hashlib
import hmac
import itertools
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from rapidfuzz import process
from rapidfuzz.distance import Levenshtein

from build_contacts import contact_id, contact_key, normalize_contact
import corpus as corpus_source
import emails as email_source
from corpus import document_urls
from normalize_graph import load_identity_merges, name_key, registry_aliases
from derived_files import replace_if_changed
from header_cache import HeaderCache
from document_prefetch import DocumentPrefetch
import header_text
from header_text import (
    BODY_AFTER_FIELD, BODY_FORM, DICTIONARY, EMAIL, ENDINGS,
    FRAGMENT_PHRASE, FRAGMENT_START, HEADER_DATE, HEADER_FIELD, HUNSPELL,
    INLINE_IMAGE_LOCAL, NAME_PARTICLES, NICKNAMES, NICKNAME_WORDS, NOT_NAME_WORDS,
    PAGE_FURNITURE, PARTY_FIELDS, PLACEHOLDER_DOMAINS, PLACEHOLDER_LOCALS, PRINTED_FIELD,
    RECIPIENT_SPLIT, SENTENCE_WORDS, SUBJECT_START, TITLE_SMALL_WORDS, _valid_email,
    attest_party, body_after_bare_field, detached_header_parties, dictionary_words, everyday_word,
    fragment_text, header_block_text, header_body_marker, name_word, nonparty_header_text,
    nonparty_values, parse_recipient, party_body_marker, plain_text, sentence_text,
    title_case, valid_email, word_key, header_junk_reason,
)
from adapters import efta as corpus_adapter
from header_context_resolution import ContextNames, compound_people, quoted_handle_documents

ROOT = Path(__file__).resolve().parents[1]


WEBMAIL_DOMAINS = ('gmail.com', 'googlemail.com', 'yahoo.com', 'hotmail.com', 'aol.com', 'icloud.com', 'outlook.com',
                   'comcast.net', 'verizon.net', 'msn.com', 'mac.com', 'me.com', 'mail.com', 'ymail.com', 'earthlink.net')
OCR_AT_SIGN = re.compile(r'\(?@\)?|®|\(a\)?')


@lru_cache(maxsize=1 << 18)
def domain_fragment(name):
    """'ail.com', 'mail.com': the tail of a webmail address split off as its own recipient."""
    text = (name or '').strip().strip('<>,;"\'').casefold()
    return len(text) >= 5 and not re.search(r'[\s@]', text) and any(domain.endswith(text) for domain in WEBMAIL_DOMAINS)


def join_split_addresses(pairs):
    """Fold webmail-domain fragments into the recipient before them.

    OCR line breaks split "Jeffrey E <jeevacation@g" | "mail.com>" into two list
    items, and the tail became a name-only participant shared by everyone whose
    address was cut there. The fragment is never a recipient of its own: when
    the item before it ends in the start of an address, the two halves are
    rejoined (only if they form a valid address); otherwise the earlier item
    already names the recipient. A fragment with nothing before it is dropped.
    """
    out = []
    for name, address in pairs:
        if address is None and domain_fragment(name):
            if out and out[-1][1] is None:
                head, _, token = out[-1][0].strip().rpartition(' ')
                token = token.lstrip('<"\'*([').rstrip()
                joined = valid_email(OCR_AT_SIGN.sub('@', token + name.strip().rstrip('>,;')))
                if joined:
                    out[-1] = (head.strip(' <"\''), joined)
            continue
        out.append((name, address))
    return tuple(out)


WRAPPED_WORD = re.compile(r"^[^\W\d_][^\W\d_'-]*(?:['-][^\W\d_]+)*$")
# Words that start a signature or sign-off ("Thanks / Lesley"), not a first name.
NOT_GIVEN_NAMES = {'thanks', 'thank', 'thx', 'thks', 'tks', 'regards', 'best', 'cheers', 'dear', 'hi', 'hello', 'love',
                   'mobile', 'direct', 'fax', 'tel', 'phone', 'cell', 'sent', 'from', 'to', 'cc', 'subject', 'date'}
MIN_WRAPPED_FULL_NAME = 20
MIN_REFERENCE_NAME = 5  # uses before a name is a reference spelling for OCR variants
DOMINANCE = 3  # a spelling with >=3x the records absorbs a rarer OCR spelling of it
SPELLING_RULES = frozenset({'header_name_one_edit', 'header_name_two_edits', 'header_name_truncated', 'header_name_ocr_skeleton',
                            'header_name_is_address', 'header_name_is_misread_address', 'header_name_mangled_address',
                            'header_name_short_first_name', 'header_name_middle_dropped', 'header_name_trailing_fragment'})


def couple_words(words):
    """Whether words read "Lynn and Jlip [Fontanilla]" (two given names, optional surname)."""
    return (len(words) in (3, 4) and words[1].casefold() in ('and', '&') and all(WRAPPED_WORD.match(w) for w in words[::2] + words[3:])
            and not {w.casefold() for w in words[::2] + words[3:]} & (FUNCTION_WORDS | NOT_GIVEN_NAMES))


def wrapped_pair(name, following):
    """'Michael Gtisvup' (or the couple 'Lynn and Jlip') when OCR wrapped one name into two
    list items, else None."""
    first, last = (name or '').strip(' ,;"\''), (following or '').strip(' ,;"\'')
    if WRAPPED_WORD.match(first) and WRAPPED_WORD.match(last) and first.casefold() not in NOT_GIVEN_NAMES:
        return f'{first} {last}'
    if couple_words(f'{first} {last}'.split()):
        return f'{first} {last}'
    return None


def join_wrapped_names(pairs, full_names):
    """Rejoin a two-word name that line wrapping split into two recipients.

    "...; Michael" | "Glidden" became two participants (a lone first name shared by
    every Michael, a lone surname). Two adjacent one-word items are joined only when
    the joined name occurs as a whole two-word header name at least
    MIN_WRAPPED_FULL_NAME times elsewhere (full_names, casefolded); the second item's
    address, if any, is kept."""
    out = []
    pairs = list(pairs)
    i = 0
    while i < len(pairs):
        name, address = pairs[i]
        if address is None and i + 1 < len(pairs):
            joined = wrapped_pair(name, pairs[i + 1][0])
            if joined and joined.casefold() in full_names:
                out.append((joined, pairs[i + 1][1]))
                i += 2
                continue
        out.append((name, address))
        i += 1
    return tuple(out)


def signed_own_name(name, address, sender_name):
    """A one-word name-only "recipient" equal to the sender's first name is the sender's
    sign-off ("Thanks! / Nadia") that leaked from the body into the recipient list."""
    word = (name or '').strip(' ,;"\'').casefold()
    first = (sender_name or '').split()
    return (address is None and len(word) >= 3 and ' ' not in word and len(first) >= 2
            and first[0].strip(' ,;"\'').casefold() == word)


def header_name_counts(rows):
    """Raw name frequencies, shared by the two header-name prepasses."""
    counts = Counter()
    for sender, to_raw, cc_raw in rows:
        counts[sender] += 1
        counts.update(name for raw in (to_raw, cc_raw) for name, _ in recipient_fields(raw) or ())
    return counts


def counted_header_names(db, *, addressed_names=None):
    """Parse each distinct recipient column once, retaining all occurrences.

    Only frequencies feed the wrapped-name and OCR-reference sets; unlike
    evidence import, these prepasses do not depend on message order.
    """
    counts = Counter(dict(db.execute('SELECT sender_name,count(*) FROM headers WHERE noise=0 GROUP BY sender_name')))
    if addressed_names is not None:
        for name, address in db.execute('SELECT DISTINCT sender_name,sender_email FROM headers WHERE noise=0'):
            name, address = parse_sender(name, address)
            if name and valid_email(address):
                addressed_names.add(name_key(name))
    recipients = Counter()
    for column in ('to_json', 'cc_json'):
        recipients.update(dict(db.execute(f'SELECT {column},count(*) FROM headers WHERE noise=0 GROUP BY {column}')))
    for raw, n in recipients.items():
        for name, address in recipient_fields(raw) or ():
            counts[name] += n
            if addressed_names is not None and name and address:
                addressed_names.add(name_key(name))
    return counts


def wrapped_full_names(rows=(), *, counts=None):
    """Casefolded two-word header names and couples ("lynn and jlip") (senders and
    recipients) seen often enough to rejoin when wrapped (join_wrapped_names).
    rows: (sender, to JSON, cc JSON)."""
    folded = Counter()
    for name, n in (header_name_counts(rows) if counts is None else counts).items():
        words = (name or '').strip(' ,;"\'').split()
        if len(words) == 2 and all(WRAPPED_WORD.match(w) for w in words) or couple_words(words):
            folded[' '.join(words).casefold()] += n
    return {name for name, n in folded.items() if n >= MIN_WRAPPED_FULL_NAME}


def normalized_date(value):
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return None


def sample_documents(db, identity):
    """Up to 3 documents of one endpoint: its own records minus those reattributed
    to someone else by document (reattributions), plus those reattributed to it."""
    moves = db.execute("SELECT 1 FROM sqlite_master WHERE name='reattributions'").fetchone()
    found = []
    for column, side in (('source_id', 'source'), ('target_id', 'target')):
        # Correlated NOT EXISTS uses the reattributions primary key; NOT IN rebuilt the
        # whole subquery on every call (~45k calls).
        moved_out = (' AND NOT EXISTS (SELECT 1 FROM reattributions r WHERE r.evidence_rowid=evidence.rowid AND r.side=?)'
                     if moves else '')
        found += [r[0] for r in db.execute(
            f'SELECT DISTINCT efta_number FROM evidence WHERE {column}=? AND efta_number IS NOT NULL{moved_out} '
            'ORDER BY efta_number LIMIT 3', (identity, side) if moves else (identity,))]
    if moves:
        found += [r[0] for r in db.execute(
            'SELECT DISTINCT e.efta_number FROM reattributions r JOIN evidence e ON e.rowid=r.evidence_rowid '
            'WHERE r.owner=? AND e.efta_number IS NOT NULL ORDER BY 1 LIMIT 3', (identity,))]
    return sorted(set(found))[:3]


def sample_references(db, source, target, kind):
    return db.execute('''SELECT efta_number,source_db,source_table,source_record_id,message_index
        FROM evidence WHERE canonical_source=? AND canonical_target=? AND relationship_type=?
        ORDER BY efta_number,source_db,source_table,source_record_id,message_index LIMIT 5''',
        (source, target, kind)).fetchall()


@lru_cache(maxsize=1 << 17)
def recipient_fields(raw):
    """Flattened parse_recipient pairs for one JSON recipient column, or None
    if it is not a JSON list. Cached: identical columns recur across millions
    of message records."""
    try:
        values = json.loads(raw or '[]')
    except (ValueError, TypeError):
        return None
    if not isinstance(values, list):
        return None
    return join_split_addresses(pair for value in values for pair in parse_recipient(value))


def parse_sender(name, address):
    """Use the structured address, else recover one embedded in the sender header
    ("Name <user@host>"), which merged rows often carry without sender_email."""
    if valid_email(address):
        return name, address
    if isinstance(name, str) and '@' in name:
        for parsed_name, email in parse_recipient(name):
            if email:
                return parsed_name, email
    return name, address


APPLE_DOMAINS = {'me.com', 'mac.com', 'icloud.com'}  # mail.mac.com: MAIL_SERVER strips 'mail.'
PROVIDERS = ('gmail', 'yahoo', 'hotmail', 'aol', 'icloud', 'outlook', 'comcast', 'verizon', 'msn', 'mac', 'me')
OCR_TLD = re.compile(r'\.(?:corn|cotn|eom|corn\w?|coml|comj|comqa|com\w|con|c0m)$')
# Endings that are no real TLD, only a misread .com (.cam is a real TLD).
COM_MISREADS = {'corn', 'cotn', 'comj', 'coml', 'comi', 'eom', 'c0m', 'coin', 'corm', 'comn', 'con'}
# Mail-server names in front of a webmail domain: jeevacation@imap.gmail.com.
MAIL_SERVER = re.compile(r'^(?:imap|smtp|pop3?|mail)\.(?=(?:gmail|googlemail|yahoo|aol|icloud|me|mac|hotmail|outlook)\.)')
MAILTO = re.compile(r'^(?:mailto|malito|mallto|maiito)[:;]?')


def edit_distance(a, b, limit=2):
    """Levenshtein distance; limit + 1 once it exceeds limit (C implementation)."""
    return Levenshtein.distance(a, b, score_cutoff=limit)


def ocr_repair(email):
    """Undo common OCR damage in an address: mailto prefixes, rn->m, garbled
    TLDs (.corn/.eom/.coml) and misread webmail providers (grnail, gmall, qmail).
    Returns (local, domain) for clustering; never used as a display value."""
    local, domain = email.casefold().rsplit('@', 1)
    local = MAILTO.sub('', local)
    domain = MAIL_SERVER.sub('', domain).replace('rn', 'm')
    base, _, tld = domain.rpartition('.')
    if base in {'grnail', 'gmad', 'gmaii', 'gmall', 'cunail', 'sinail', 'qmail', 'gmair', 'innail', 'umail'}:
        base = 'gmail'
    # mail.ru / email.it are real mail services, not a misread gmail (gmail.co and mail.com still are).
    own_service = base in ('mail', 'email') and len(tld) == 2 and tld.isalpha() and edit_distance(tld, 'com', 1) > 1
    for provider in PROVIDERS:
        if not own_service and len(provider) >= 4 and base != provider and edit_distance(base, provider, 2) <= (1 if len(provider) < 5 else 2):
            base = provider
            break
    # Gmail only exists at .com, so any other ending is damage (gmail.co cut off at a line
    # end, gmail.oam); other providers: one edit from .com (aol.co, me.cm, hotmail.cpm).
    if base in ('gmail', 'googlemail') or base in PROVIDERS and tld != 'com' and edit_distance(tld, 'com', 1) <= 1:
        tld = 'com'
    domain = base + '.' + tld if base else domain
    if base in PROVIDERS:
        domain = OCR_TLD.sub('.com', domain)
    elif base and tld in COM_MISREADS:  # any company: db.comj, corcoran.corn, bakerbotts.corm
        domain = base + '.com'
    if domain in ('gmail.com', 'googlemail.com'):  # Gmail ignores dots and +tags: one mailbox
        local, domain = local.split('+', 1)[0].replace('.', ''), 'gmail.com'
    return local, domain


ADDRESS_LABEL_RULES = {'header_name_mangled_address', 'header_name_is_misread_address'}


def non_mailbox_reason(value):
    """Transport identifiers stay in the graph, hidden as not_a_name."""
    if '@' not in value:
        return None
    local, domain = value.casefold().rsplit('@', 1)
    if re.fullmatch(r'image\d+\.(?:png|gif|jpe?g|bmp|tiff?|webp)', local):
        return 'mime_content_id'
    if domain.startswith(('mms.', 'sms.')):
        return None  # Numeric SMS gateway recipients are real contact addresses.
    if (domain.endswith(('.home', '.local')) or re.fullmatch(r'\d[\d._-]{7,}', local)
            or re.fullmatch(r'discard\.maildb\.(?:com|coml|comi|corn)', domain)):
        return 'message_id'
    return None


def non_mailbox_contact_nodes(rows, key):
    """Quality overlays for raw extracted contacts absent from parsed headers.

    In particular, the header parser already rejects MIME Content-IDs, but the
    extracted-contact layer retains them. Keep those nodes and hide them too.
    """
    nodes = {}
    for row in rows:
        value = normalize_contact(row.get('entity_value', ''), 'email')
        if value and non_mailbox_reason(value):
            identity = contact_id(value, 'email', key)
            nodes[identity] = dict(id=identity, name='Email_' + identity.split('_', 1)[1],
                                   entity_type='email', label_quality='not_a_name')
    return list(nodes.values())


def extracted_contact_addresses(rows, key):
    """Include contacts extracted outside headers in the same identity map."""
    values = {}
    for row in rows:
        value = normalize_contact(row.get('entity_value', ''), 'email')
        if value:
            values[contact_id(value, 'email', key)] = value
    return values


def address_family_merges(email_values, counts):
    """Frozen mailbox references; require 10x evidence and a unique best local part.

    Require the same repaired domain and retain different numeric account
    suffixes (a second account is often intentional). No fuzzy chains.
    """
    domains = defaultdict(list)
    for identity, value in email_values.items():
        if '@' in value and not non_mailbox_reason(value):
            local, domain = ocr_repair(value)
            domains[domain].append((counts[identity], local, identity))
    merges = {}
    for items in domains.values():
        owners = {}
        for count, local, identity in sorted(items, key=lambda x: (-x[0], x[2])):
            owners.setdefault(local, (count, identity))
        frequent = []  # An OCR variant cannot itself become a fuzzy reference.
        for count, local, identity in sorted(items, key=lambda x: (-x[0], x[2])):
            if owners[local][1] != identity:
                merges[identity] = (owners[local][1], 'same_repaired_mailbox')
                continue
            if len(local) < 6:
                if count >= 10:
                    frequent.append((local, count, identity))
                continue
            hits = []
            for other, n, target in frequent:
                if n < 10 * max(count, 1) or identity == target:
                    continue
                # Do not erase plausible numbered accounts, even at a damaged domain.
                number = lambda text: (re.search(r'\d+$', text)[0] if re.search(r'\d+$', text) else '')
                if number(local) != number(other):
                    continue
                distance = edit_distance(local, other, 2)
                if distance <= (2 if min(len(local), len(other)) >= 8 else 1):
                    hits.append((distance, target, 'local_edits'))
                elif len(local) >= 6 and (other.startswith(local) or other.endswith(local)):
                    hits.append((3, target, 'local_truncation'))
                elif len(other) >= 8 and len(local) > len(other):
                    # Prefix, suffix, repetition, or two damaged mailbox readings glued
                    # together. The complete anchor must occur at an end, within 1 edit.
                    if min(edit_distance(local[:len(other)], other, 1),
                           edit_distance(local[-len(other):], other, 1)) <= 1:
                        hits.append((4, target, 'local_junk_affix'))
                    else:
                        for lost in (1, 2):
                            kept = len(other) - lost
                            if kept >= 6 and (local.endswith(other[-kept:]) or local.startswith(other[:kept])):
                                hits.append((4 + lost, target, 'local_junk_affix'))
                                break
            if hits:
                # A common OCR spelling can itself survive as a reference. It
                # must not tie/beat an anchor with 10x its evidence when BOTH
                # are direct matches to this address (never a fuzzy chain).
                candidates = {target for _, target, _ in hits}
                dominant = max(candidates, key=lambda i: (counts[i], i))
                if len(candidates) > 1 and counts[dominant] >= 10 * max(counts[i] for i in candidates - {dominant}):
                    hits = [h for h in hits if h[1] == dominant]
                best = min(h[0] for h in hits)
                nearest = {target for score, target, _ in hits if score == best}
                if len(nearest) == 1:
                    merges[identity] = (next(iter(nearest)), min(reason for score, _, reason in hits if score == best))
            if identity not in merges and count >= 10:
                frequent.append((local, count, identity))
    return merges


def domain_typo_merges(email_values, counts=None, min_frequent=5):
    """{identity: target} for addresses at a rare domain that is OCR damage of a common one.

    Domains are counted over all addresses (after ocr_repair), like a dictionary of
    domains. A domain seen on at most two addresses maps to the unique nearest domain
    seen on >= min_frequent and >= 10 times as many addresses (one edit, two for
    domains of 10+ characters; short domains like nbc.com/db.com never qualify). The
    address merges only into the same local part at that domain (hmflaw.co ->
    hmflaw.com, uscourts.go -> .gov, flighlaware -> flightaware)."""
    counts = counts or Counter()
    repaired = {identity: ocr_repair(value) for identity, value in email_values.items() if '@' in value}
    addresses = Counter(domain for _, domain in repaired.values())
    frequent = [d for d, n in addresses.items() if n >= min_frequent]
    owner = {}  # (local, domain) -> busiest identity
    for identity, key in sorted(repaired.items(), key=lambda row: (-counts[row[0]], row[0])):
        owner.setdefault(key, identity)
    choices = {n: [f for f in frequent if addresses[f] >= 10 * n] for n in (1, 2)}
    fixed = {}
    for domain, n in addresses.items():
        if n > 2 or len(domain) < 6 or '.' not in domain:
            continue
        limit = 2 if len(domain) >= 10 else 1
        # RapidFuzz scans in C and stops at the two nearest matches. Two
        # equally near domains remain ambiguous, just as in the full ranking.
        near = process.extract(domain, choices[n], scorer=Levenshtein.distance,
                               score_cutoff=limit, limit=2)
        if near and (len(near) == 1 or near[1][1] > near[0][1]):
            fixed[domain] = near[0][0]
    merges = {}
    for identity, (local, domain) in repaired.items():
        target = owner.get((local, fixed.get(domain)))
        if target and target != identity:
            merges[identity] = target
    return merges


# Local parts of a company's automated or role mailboxes (noreply@, messages-noreply@,
# info@, order-update@): the company writing, not a person.
AUTOMATED_LOCAL = re.compile(
    r'^(?:[a-z]+[-_])?(?:no-?reply|do-?not-?reply|donotreply|notifications?|notify|info|news|newsletters?|editor|'
    r'messages?|mailer|alerts?|updates?|support|service|services|hello|team|marketing|members?|accounts?|billing|'
    r'orders?|order-update|shop|store|help|contact|digest|invitations?|invite|feedback|offers|deals|promo|sales|'
    r'customer-?service|security|auto-?confirm|confirm|reservations?|receipts?|account-services|bookings?|dispatch|concierge|(?:pin|ro)?bot)(?:[-_.+][\w.+-]*)?$')
SECOND_LEVEL = {'co', 'com', 'org', 'net', 'ac', 'gov', 'edu'}


def automated_company(value):
    """'linkedin.com' for messages-noreply@bounce.linkedin.com, None for a personal address
    or any webmail provider's (mailer-daemon@googlemail.com is not a company sender)."""
    if '@' not in (value or ''):
        return None
    local, domain = ocr_repair(value)
    labels = domain.split('.')
    if len(labels) < 2 or not AUTOMATED_LOCAL.match(local):
        return None
    keep = 3 if len(labels) >= 3 and labels[-2] in SECOND_LEVEL and len(labels[-1]) == 2 else 2
    company = '.'.join(labels[-keep:])
    company = re.sub(r'^bounce(?=[a-z]{4,}\.)', '', company)  # OCR lost the dot: bouncelinkedin.com
    base = company.split('.')[0]
    if company in WEBMAIL_DOMAINS or base in PROVIDERS or base in ('att', 'comcast', 'verizon', 'earthlink', 'msn', 'live'):
        return None
    return company


def gmail_mailbox(email):
    """The mailbox a clean Gmail address delivers to (dots and +tags ignored), else None."""
    local, domain = email.casefold().rsplit('@', 1)
    return local.split('+', 1)[0].replace('.', '') if domain in ('gmail.com', 'googlemail.com') else None


def name_skeleton_words(name):
    """Words of a name with OCR look-alikes folded: l/I/i/1/|/j/! -> l, 0 -> o,
    q -> g, rn -> m, vv -> w; accents, apostrophes, dots and hyphens dropped."""
    text = unicodedata.normalize('NFKD', name or '').encode('ascii', 'ignore').decode().casefold()
    text = re.sub(r"['’`.]", '', text).replace('rn', 'm').replace('vv', 'w')
    text = text.translate(str.maketrans({'1': 'l', 'i': 'l', '|': 'l', '!': 'l', 'j': 'l', '0': 'o', 'q': 'g'}))
    return re.findall(r'[a-z]+', text)


def name_skeletons(name):
    """Keys shared by spellings of one name. An apostrophe may be a misread i
    (iv'et / iviet). Multi-word names also match in any word order; single
    words only match a name written as one word ("jeanluc" / "jean luc")."""
    keys = set()
    plain = re.findall(r'[a-z0-9]+', unicodedata.normalize('NFKD', name or '').encode('ascii', 'ignore').decode().casefold())
    if sum(map(len, plain)) >= 3 and '?' not in (name or ''):
        keys.add('plain:' + ''.join(plain))  # "yfke -" / "yfke": punctuation only
    for apostrophe in ('', 'i'):
        words = name_skeleton_words(re.sub(r"['’`]", apostrophe, name or ''))
        if sum(map(len, words)) < 5 or '?' in (name or ''):
            continue
        keys.add('joined:' + ''.join(words))
        if len(words) >= 2:
            keys.add('sorted:' + ' '.join(sorted(words)))
    return keys


GENERIC_TLDS = {'com', 'org', 'net', 'edu', 'gov', 'mil', 'int', 'info', 'biz', 'name', 'pro', 'aero', 'coop',
                'museum', 'mobi', 'asia', 'tel', 'travel', 'jobs', 'cat', 'xxx', 'law', 'nyc', 'london', 'io'}


def ocr_artifacts(email):
    """Visible OCR damage in an address, e.g. ['garbled_tld', 'misspelled_provider']."""
    local, domain = email.casefold().rsplit('@', 1)
    base, _, tld = domain.rpartition('.')
    found = []
    if MAILTO.match(local):
        found.append('mailto_prefix')
    if base not in PROVIDERS and any(len(p) >= 4 and edit_distance(base.replace('rn', 'm'), p, 2) <= (1 if len(p) < 5 else 2) for p in PROVIDERS):
        found.append('misspelled_provider')
    if not (tld in GENERIC_TLDS or re.fullmatch(r'[a-z]{2}', tld)) or (ocr_repair(email)[1].endswith('.com') and tld != 'com'):
        found.append('garbled_tld')
    return found


# Header text that is not a name (from a node-by-node audit of the viewer).
BOILERPLATE_TOKENS = {
    'regards', 'greetings', 'thanks', 'thank', 'thx', 'untitled', 'attachment', 'attachments', 'subject', 'subj', 'subjec',
    'reply', 'replyto', 'mailto', 'mailt', 'calendar', 'appointment', 'itinerary', 'locator', 'traveler',
    'information', 'dear', 'hi', 'hello', 'please', 'view', 'balance', 'amount', 'fax', 'tel', 'email', 'e-mail',
    'forwarded', 'message', 'iphone', 'ipad', 'blackberry', 'sent', 'conversation', 'gmail', 'remote', 'mailbox',
    'prediction', 'assurance', 'constitute', 'reserved', 'photojpg', 'advise', 'arrangements', 'details',
    'disclaimer', 'confidential', 'privileged', 'intended', 'recipient', 'unsubscribe', 'newsletter', 'noreply'}
FUNCTION_WORDS = {'and', 'or', 'the', 'of', 'in', 'to', 'from', 'your', 'you', 'are', 'is', 'be', 'by', 'at',
                  'for', 'with', 'where', 'when', 'who', 'what', 'yes', 'no', 'ok', 'an', 'a', 'this', 'that', 'my', 'me'}
WEEKDAYS = {'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'}
ROLE_TOKENS = {'assistant', 'office', 'president', 'director', 'managing', 'legal', 'airline', 'team', 'desk',
               'department', 'operations', 'oversight', 'ops', 'admin', 'reception', 'concierge', 'staff', 'family', 'enterprise',
               'enterprises', 'services', 'group', 'support', 'travel', 'flight', 'secretary', 'household'}
# Single words that are header text, not names. (A dictionary-based rule was
# tried and rejected: it dropped first names such as Larry, Robert, Sarah.)
NON_NAME_WORDS = {'com', 'wrote', 'rote', 'mobile', 'phone', 'office', 'president', 'director', 'associate', 'sincerely',
                  'usa', 'cell', 'home', 'work', 'fw', 'fwd', 're', 'info', 'admin', 'support', 'private',
                  'personal', 'unknown', 'none', 'null', 'cc', 'bcc', 'sender', 'recipient', 'best', 'cheers',
                  'xxx', 'xx', 'rsvp', 'importance', 'high', 'normal', 'low', 'priority', 'inbox', 'sent', 'draft'}


def node_review():
    path = ROOT / 'config/node_review.json'
    return json.loads(path.read_text()) if path.exists() else {'participants': {}, 'persons': {}}


DROP_VERDICTS = {'boilerplate', 'ocr_garbage', 'redaction_or_placeholder'}


def is_phrase(label):
    """Four or more words (initials and particles not counted) are a phrase that may
    hold a name ("Sigrid S. McCawley - Boies", "just left you a voicemail"), not a name."""
    words = re.findall(r"[^\W\d_]+", label.casefold())
    return sum(len(w) > 1 and w not in NAME_PARTICLES for w in words) >= 4


def label_quality(label):
    """'drop' (boilerplate/OCR noise), 'group_or_role' or 'ok' for a header name."""
    if header_junk_reason(label):
        return 'not_a_name'  # retain the extracted identity, hide its bad label
    words = re.findall(r"[^\W\d_]+", label.casefold())
    if not words:
        return 'drop'
    if re.fullmatch(r'[a-z]+(?:-[a-z]+){2,}', label.casefold().strip()):
        return 'drop'  # mail-client field names: "date-last-viewed", "original-mailbox"
    if any(w in BOILERPLATE_TOKENS for w in words) or (words[0] == 'on' and len(words) > 1 and words[1] in WEEKDAYS):
        return 'drop'
    function = sum(w in FUNCTION_WORDS for w in words)
    if (len(words) == 1 and function) or (function >= 2 and function * 2 >= len(words)):
        return 'drop'  # "where are you", "and the information"; not "A Brudorb"
    short = [w for w in words if len(w) <= 2 and w not in NAME_PARTICLES]
    # Two-letter fragments, not initials: "oog e a en ar" drops, "J R R Smith" stays.
    if len(words) >= 4 and len(short) >= 3 and sum(len(w) == 2 for w in short) >= 2 and len(short) * 2 >= len(words):
        return 'drop'  # "oog e a en ar" (recoverable names are matched before this runs)
    if all(re.fullmatch(r'[ilmn]*[il]{2,}[ilmn]*', w) for w in words) or len(''.join(words)) <= 2:
        return 'drop'  # "III", "MINIII", "fr", "Mr"; initials like "LSJ"/"WJC" are kept
    if len(words) == 1 and words[0] in NON_NAME_WORDS:
        return 'drop'
    if any(w in ROLE_TOKENS for w in words):
        return 'group_or_role'
    return 'ok'


def plain_words(spellings, name_words=frozenset()):
    """True when every spelling of a header name is plain English text ("dissemination",
    "Requested items", "You're correct"). Kept and hidden as 'not_a_name', not dropped.
    Proper nouns (Larry, May, Rose), nicknames, words of a known person's name and
    Title Case names ("Dept of Neurology") do not count, unless the words make a sentence."""
    return bool(spellings and dictionary_words()[0]) and all(plain_text(s, name_words) for s in spellings)


def body_name_exempt(name, builder, addressed_names, name_variant):
    """Shared safeguards for detecting body text in header names, including reviewed roles/places."""
    return (name_key(name) in addressed_names or builder.known_name(name) is not None
            or builder.review.get(builder.participant_id(name_key(name)), {}).get('verdict') in REVIEWED_NAMES | {'group_or_role', 'place'}
            or name_variant(name))


def name_letters(label):
    return ' '.join(re.sub(r'[^a-z ]', '', label.casefold()).split())


def frequent_names(rows=(), *, counts=None):
    """{length: names} of two- and three-word header names seen often enough to be reliable
    spellings, as name_letters. rows: (sender, to JSON, cc JSON). Plain text and
    explicit form labels do not count: repeated boilerplate cannot exempt itself."""
    by_length = defaultdict(set)
    for name, n in (header_name_counts(rows) if counts is None else counts).items():
        if (isinstance(name, str) and 2 <= len(name.split()) <= 3 and n >= MIN_REFERENCE_NAME
                and not plain_text(name) and not BODY_FORM.match(name)):
            letters = name_letters(name)
            by_length[len(letters)].add(letters)
    return by_length


def ocr_name_variant(label, names):
    """One or two OCR edits from a frequent name ("story cowle", "I n Fontanilla", "Russell
    Her=andez"), whose words can read as text. Only full names: a lone "Story" or "Sultan"
    is as often a signature."""
    letters = name_letters(label)
    if not 2 <= len(letters.split()) <= 3 or len(letters) < 8:
        return False
    limit = 2 if len(letters) >= 12 else 1
    # extractOne runs the entire candidate loop in C, with the same exact
    # Levenshtein cutoff as edit_distance. Only existence matters, never ties.
    return any(process.extractOne(letters, names.get(size, ()), scorer=Levenshtein.distance,
                                  score_cutoff=limit) is not None
               for size in range(len(letters) - limit, len(letters) + limit + 1))


HONORIFICS = {'mr', 'mrs', 'ms', 'miss', 'dr', 'prof', 'sir', 'dame', 'lady', 'lord', 'hon'}
CREDENTIALS = {'md', 'phd', 'esq', 'jr', 'sr', 'ii', 'iii', 'cpa', 'jd', 'mba', 'pa', 'llp', 'pc'}


def name_tokens(label):
    """Header name -> (given, surname, middle) tokens, without honorifics,
    credentials or trailing OCR fragments ("Lesley Groff a", "Kahn c")."""
    if label.count(',') == 1:
        last, first = label.split(',')
        label = f'{first} {last}'
    words = [w for w in re.findall(r"[^\W\d_]+", label.casefold()) if w not in HONORIFICS and w not in CREDENTIALS]
    while len(words) > 2 and len(words[-1]) == 1 and len(words[-2]) >= 4:
        words.pop()  # A lone OCR tail, not a legitimate short surname (Li, Ng, Wu).
    return words


def is_subsequence(short, long_):
    if not short or not long_ or short[0] != long_[0] or len(short) > len(long_):
        return False
    position = 0
    for c in short:
        position = long_.find(c, position) + 1
        if not position:
            return False
    return True


def given_compatible(a, b):
    a, b = NICKNAMES.get(a, a), NICKNAMES.get(b, b)
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return (len(short) == 1 and long_.startswith(short)) or (len(short) >= 3 and long_.startswith(short))


class PersonMatcher:
    """Resolve header names to exactly one graph person, recording the rule.

    Rules: exact after cleanup; reordered surname/given; given-name
    initial/short form/nickname with the same surname (middle names ignored);
    one OCR edit for long names (two for >= 14 letters). Ambiguous labels (two
    people with that surname and compatible given names) stay unmerged.
    """

    def __init__(self, entities):
        self.by_surname = defaultdict(list)
        self.people = []
        self.cache = {}
        for e in entities:
            if e['entity_type'] != 'person' or e.get('suspected_parse_artifact'):
                continue
            words = name_tokens(e['name'])
            if len(words) >= 2 and len(words[-1]) >= 2:
                self.by_surname[words[-1]].append((words, e['id']))
                self.people.append((' '.join((words[0], words[-1])), e['id']))
        # OCR-spelling candidates by length: only names within the edit limit
        # of a label's length can match, so the full scan is never needed.
        self.people_by_length = defaultdict(list)
        for full, entity_id in self.people:
            self.people_by_length[len(full)].append((full, full.rsplit(' ', 1)[-1], entity_id))
        self.compact = [(re.sub(r'[^a-z]', '', ''.join(name_tokens(e['name']))), e['id']) for e in entities
                        if e['entity_type'] == 'person' and not e.get('suspected_parse_artifact') and len(name_tokens(e['name'])) >= 2]
        # ligature_loss candidates by first letter (a subsequence must share it),
        # with the in/rn/ni -> m form precomputed.
        self.ligature_forms = defaultdict(list)
        for full, entity_id in self.compact:
            for form in {full, re.sub(r'in|rn|ni', 'm', full)}:
                if form:
                    self.ligature_forms[form[0]].append((len(full), form, entity_id))
        joined = Counter(given + surname for (given, *_rest, surname), _ in ((w, i) for group in self.by_surname.values() for w, i in group))
        self.concatenated = {given + surname: entity_id for group in self.by_surname.values()
                             for (given, *_rest, surname), entity_id in group if joined[given + surname] == 1 and len(given + surname) >= 8}

    def match(self, label):
        # The same garbled labels recur hundreds of thousands of times.
        if label not in self.cache:
            self.cache[label] = self._match(label)
        return self.cache[label]

    def _match(self, label):
        words = name_tokens(label)
        if len(words) == 1 and words[0] in self.concatenated:
            return self.concatenated[words[0]], 'concatenated'  # "bellaklein"
        if len(words) < 2:
            return self.ligature_loss(label)
        found = {}
        for given, surname in ((words[0], words[-1]), (words[-1], words[0])):
            for person, entity_id in self.by_surname.get(surname, ()):
                if given_compatible(given, person[0]):
                    rule = 'exact' if words == person else 'reordered' if surname == words[0] else 'given_name_variant'
                    found.setdefault(entity_id, rule)
        if not found and len(words) > 2 and len(words[-1]) > 3:
            # Recover multi-word header tails ("Jeffrey Epstein New York"),
            # but never discard just one word: it may be the actual surname.
            for size in (3, 2):
                if len(words) - size < 2:
                    continue
                entity_id, rule = self.match(' '.join(words[:size]))
                if entity_id is not None:
                    return entity_id, 'leading_name_' + rule
        if not found:
            compact = ' '.join((words[0], words[-1]))
            limit = 2 if len(compact) >= 14 else 1 if len(compact) >= 10 else 0
            for length in range(len(compact) - limit, len(compact) + limit + 1) if limit else ():
                for full, surname, entity_id in self.people_by_length.get(length, ()):
                    if min(len(words[-1]), len(surname)) <= 3 and words[-1] != surname:
                        continue  # A long given name does not justify changing Li into Ng.
                    if edit_distance(compact, full, limit) <= limit:
                        found.setdefault(entity_id, 'ocr_spelling')
        if not found:
            return self.ligature_loss(label)
        if len(found) != 1:
            return None, None
        (entity_id, rule), = found.items()
        return entity_id, rule

    def ligature_loss(self, label):
        """Last resort: OCR dropped ligatures (ff, fi, pt) and read in/rn as m,
        e.g. "je rey eps em" = Jeffrey Epstein. Letters must be an in-order subset
        of exactly one person's name, cover >= 65% of it and share the first letter."""
        # Untrimmed letters: tail trimming would cut "eps em" off "je rey eps em".
        letters = ''.join(w for w in re.findall(r'[^\W\d_]+', label.casefold()) if w not in HONORIFICS)
        if len(letters) < 8:
            return None, None
        hits = {i for size, form, i in self.ligature_forms.get(letters[0], ())
                if len(letters) >= 0.65 * size and is_subsequence(letters, form)}
        # A word that is another person's whole surname names them, not a garbled
        # name it happens to fit inside ("Lord Mandelson" in "mandyellison").
        words = [w for w in name_tokens(label) if w not in HONORIFICS]
        if any(len(w) >= 4 and self.by_surname.get(w) and not hits & {i for _, i in self.by_surname[w]} for w in words):
            return None, None
        return (next(iter(hits)), 'ligature_loss') if len(hits) == 1 else (None, None)


GENERATIONAL = {'ii', 'iii', 'iv', 'jr', 'sr'}


def strip_trailing_fragments(spelling):
    """'Lesley Groff cza' -> 'Lesley Groff': drop up to two trailing words that
    cannot be a surname as written, OCR of the address bracket or "[mailto:" after
    the name: punctuation at either end ('fr-', '..tj', '-MINIIMM'), odd case
    ('cO', 'halltgiMMI', 'JIMIll'), 'mailto' misread ('imailto'), or up to three
    letters: one letter, lowercase / all capitals after capitalized name words ('cza', 'AOL'). Title-case
    words stay ('Shing Tung Yau', 'Lesley Groff Mia'), as do McDonald, Rod-Larsen and the
    generational suffixes II, III, IV, Jr, Sr."""
    words = spelling.split()
    capitalized = bool(words) and words[0][:1].isupper()
    for _ in range(2):
        if len(words) <= 2:
            break
        token = words[-1]
        letters = re.sub(r'[\W\d_]', '', token)
        odd_case = bool(letters) and ((letters[0].islower() and not letters.islower())
                                      or re.search(r'[A-Z]{2,}[a-z]|[a-z][A-Z].*[a-z][A-Z]|[a-z][A-Z]+$', letters) is not None)
        junk = (not letters or not token[0].isalpha() or not (token[-1].isalpha() or token[-1] == '.')
                or odd_case or 'mallto' in ''.join(name_skeleton_words(token)))
        if token.rstrip('.,').casefold() in GENERATIONAL:  # 'Joseph Kennedy III' is not 'Joseph Kennedy'
            break
        if len(letters) <= 3 and not junk:  # short words only: 'Bill Gates foundation' keeps its last word
            junk = len(letters) <= 1 or (capitalized and (letters.islower() or (letters.isupper() and not all(w.isupper() for w in words[:-1]))))
        if not junk:
            break
        words.pop()
    return ' '.join(words)


# "Sigrid S. McCawley - Boies", "Ykeh Nma I Cellist": '|' is mostly read as I or l.
TAIL_SEPARATORS = {'-', '–', '—', '|', 'I', 'l', '/', '•', '·', '@'}
TITLE_WORDS = {'president', 'partner', 'controller', 'architect', 'cellist', 'consultant', 'founder', 'cofounder',
               'ceo', 'cfo', 'coo', 'cto', 'chairman', 'chairwoman', 'principal', 'counsel', 'attorney', 'esq', 'md',
               'phd', 'designer', 'photographer', 'producer', 'artist'}
# Tails that make the label someone else's mailbox, a company or a campaign:
# "Woody Allen Assistant", "Peggy Siegal Company", "Philip Levine for Mayor".
SEPARATE_TAIL_WORDS = (ROLE_TOKENS - {'president', 'director', 'managing'}) | {
    'asst', 'campaign', 'mayor', 'foundation', 'studio', 'studios', 'company', 'co', 'inc', 'llc', 'pllc', 'llp',
    'ltd', 'corp', 'associates', 'partners', 'and', 'chief', 'scheduler', 'scheduling', 'friend', 'wife', 'husband',
    'mother', 'father', 'son', 'daughter', 'mom', 'dad', 'trust', 'estate', 'law', 'firm', 'pa', 'pc',
    'accounting', 'accounts', 'billing', 'comptabilite', 'behalf', 'victim', 'victims', 'plaintiff', 'defendant', 'vs'}
LEGAL_FORMS = {'llc': 'llc', 'l l c': 'llc', 'inc': 'inc', 'incorporated': 'inc', 'ltd': 'ltd', 'limited': 'ltd',
               'lp': 'lp', 'l p': 'lp', 'llp': 'llp', 'pllc': 'pllc', 'corp': 'corp', 'corporation': 'corp', 'plc': 'plc',
               'gmbh': 'gmbh', 'sa': 'sa', 'ag': 'ag', 'sarl': 'sarl', 'nv': 'nv', 'n v': 'nv', 'bv': 'bv'}
LEGAL_FORM_RE = re.compile(r'[\s,]+(' + '|'.join(sorted(map(re.escape, LEGAL_FORMS), key=len, reverse=True)) + r')$')
# A form-less header name only merges into a company when it reads as one ("Mitchell
# Holdings"); "Jeff Koons" / Jeff Koons LLC are the artist and his company.
COMPANY_WORDS = {'associates', 'holdings', 'holding', 'financial', 'capital', 'management', 'partners', 'development',
                 'aviation', 'company', 'group', 'bank', 'securities', 'services', 'interiors', 'equities', 'gateway',
                 'investments', 'properties', 'advisors', 'advisory', 'realty', 'ventures', 'enterprises', 'consulting',
                 'industries', 'airways', 'air', 'aircraft', 'international', 'global', 'fund', 'funds', 'media', 'design',
                 'trading', 'land', 'construction', 'yachts', 'motors', 'systems', 'technologies', 'solutions'}


def legal_form(label):
    """(name words without a trailing legal form, the form or ''): "The McClatchy
    Company, LLC" -> ("the mcclatchy company", "llc"). Base '' for one-word names."""
    text = re.sub(r'[^a-z0-9& ]', ' ', label.casefold()).strip()
    text = re.sub(r'\s+', ' ', text)
    m = LEGAL_FORM_RE.search(' ' + text)
    form = LEGAL_FORMS[m.group(1)] if m else ''
    base = (' ' + text)[:m.start()].strip() if m else text
    return (base if len(base.split()) >= 2 else ''), form


HEAD_NOISE = re.compile(r'^(?:(?:(?i:hello|hey|dear|thx|thanks|thank\s+you|xoxo|xo|good\s+(?:morning|afternoon|evening))|Hi|hi)(?:[,:;!.]?\s+|[,:;!]\s*)'
                        r'|(?i:external|cc|bcc|fw|fwd|alert)\s*[-:]\s*|(?i:external|cc|bcc)\s+|(?i:p\.\s?o\.)\s+(?!(?i:box|address)\b)'
                        r'|[-•*>|.,~]+\s*)')


def strip_name_head(spelling):
    """'Hello Lynne' -> 'Lynne', 'EHAGANDI-Jtis Ckeloc' -> 'John Ckeloc', 'Cc Rnu Btahad' -> 'Rnu Btahad',
    '-Napoleon Hill' -> 'Napoleon Hill': a greeting, mail tag or stray mark before a capitalized name."""
    match = HEAD_NOISE.match(spelling)
    if not match:
        return None
    core = spelling[match.end():].strip(' ,;:-')
    return core if core[:1].isupper() and core != spelling else None


def strip_name_tail(spelling):
    """'Sigrid S. McCawley - Boies' -> ('Sigrid S. McCawley', 'separator', 'Boies'): a name
    followed by a firm, a title or OCR noise, judged on the spelling as written.
    Tails: anything after a separator (- | I / @); or one last word that is a title
    ('John Hner Adorkerus'), lowercase after a capitalized name ('Eva Dubin czli'),
    bracket strokes ('Lesley Groff MINIM') or the start of a name word repeated
    ('Larry Visoski Lar'). None when the tail names a mailbox, company, campaign or
    relative (SEPARATE_TAIL_WORDS), holds 'and', or is a real-looking surname
    ('Tceco Hsigan Coleman', 'Lisa Emelia Svensson': left to review)."""
    tokens = spelling.split()
    if len(tokens) < 3:
        return None
    upper = all(not w.isalpha() or w.isupper() for w in tokens)
    for k in range(2, len(tokens) - 1):
        token = tokens[k]
        if token in ('I', 'l') and not (tokens[k + 1][:1].isupper() and all(w[:1].isupper() for w in tokens[:k] if w[:1].isalpha())):
            continue  # the pronoun: 'not sure I understand', 'Until then I will'
        if token in TAIL_SEPARATORS or (token == 'II' and upper):  # 'JESSICA BFOSO II CO-FOUNDER'
            core, tail, kind = tokens[:k], tokens[k + 1:], 'separator'
            break
    else:
        core, tail = tokens[:-1], tokens[-1:]
        word = tail[0]
        letters = re.sub(r'[\W\d_]', '', word)
        folded = letters.casefold()
        capitalized = all(w[:1].isupper() for w in core if w[:1].isalpha())
        if folded in TITLE_WORDS:
            kind = 'title'
        elif len(letters) >= 4 and letters.islower() and capitalized and word.isalpha():
            kind = 'lowercase_noise'
        elif len(letters) >= 5 and re.fullmatch(r'[ilmnhu]+', folded) and letters.isupper():
            kind = 'bracket_strokes'
        elif len(folded) >= 3 and folded.isalpha() and any(
                len(w) > len(folded) and w.startswith(folded) for w in name_skeleton_words(' '.join(core))):
            kind = 'repeated_name_start'
        else:
            return None
    tail_words = set(re.findall(r'[a-z]+', ' '.join(tail).casefold().replace('-', '')))  # 'co-founder' is no 'co'
    if not tail_words or tail_words & SEPARATE_TAIL_WORDS or '&' in tail:
        return None
    if core[-1].rstrip('.,').casefold() in GENERATIONAL:
        return None  # 'Martin B. O'Ckemar II Esq' is not Martin O'Ckemar
    return ' '.join(core), kind, ' '.join(tail)


def family_name_words(label):
    """Literal name words, retaining OCR digits and surname gaps; no nickname folding."""
    text = unicodedata.normalize('NFKD', label or '').encode('ascii', 'ignore').decode().casefold()
    if text.count(',') == 1:
        last, first = text.split(',')
        text = first + ' ' + last
    text = re.sub(r"['`.]", '', text)
    words = re.findall(r'[a-z01]+', text.translate(str.maketrans({'|': 'l', '!': 'l'})))
    return tuple(w for w in words if w not in HONORIFICS)


@lru_cache(maxsize=1 << 17)
def family_ocr_key(text):
    """Only the specified visual confusions, unlike the broader legacy skeleton."""
    # Expand to strokes rather than greedily collapsing digraphs: folding li -> h
    # before i -> l would make Lillian and Iillian different keys.
    return text.translate(str.maketrans({'i': 'l', '1': 'l', '0': 'o', 'm': 'rn', 'd': 'cl', 'h': 'll'}))


# Letters OCR confuses, as groups: a one-letter change inside a group is a
# misreading (Markin/Merkin), outside it two real surnames (Mailer/Miller,
# Haig/Hall, Toth/Roth).
OCR_CONFUSABLE = str.maketrans({c: g[0] for g in ('ltfij1!|', 'ceoa0', 'un', 'gqy9', 'bh6', 's5', 'z2', 'B8') for c in g})


def confusable_equal(a, b):
    """Same letters up to OCR look-alikes ('Jabor'/'labor', 'Erik'/'Etik'), case and spacing aside."""
    # r/t only inside a word (Erik/Etik); a first letter keeps Roth and Toth apart.
    fold = lambda t: ''.join(re.sub(r'(?<=\w)r', 't', w) for w in re.findall(r'[0-9a-z!|]+', t.casefold())).translate(OCR_CONFUSABLE)
    return a.casefold() != b.casefold() and fold(a) == fold(b)


def _given_names():
    from entity_resolution import FIRST_NAMES
    return set(FIRST_NAMES) | set(NICKNAMES) | set(NICKNAMES.values())


@lru_cache(maxsize=1 << 16)
def dominant_spelling_ok(rule, own, other):
    """May a spelling-level rule move `own` into a >=3x more frequent `other`?

    Only for misreadings: a substitution must stay within OCR look-alike
    groups, a cut first word must not itself be a real given name (Anna is not
    a cut Brianna), an address match must share a word, and initial-only or
    reordered labels never qualify.
    """
    own_words, other_words = family_name_words(own), family_name_words(other)
    if not own_words or not other_words:
        return False
    if rule in ('header_name_one_edit', 'header_name_two_edits', 'header_name_ocr_family:surname_edits',
                'header_name_variant:ocr_spelling', 'header_name_ocr_family:surname_one_ocr_edit'):
        a, b = (' '.join(w).translate(OCR_CONFUSABLE) for w in (own_words, other_words))
        if a == b or a.replace(' ', '') == b.replace(' ', ''):
            return True
        # A letter lost to a gap: 'ma ur' is 'mayur', 'jas er' is 'jasper'.
        return any(len(b) == len(a) and a[:i] == b[:i] and a[i + 1:] == b[i + 1:]
                   for i, c in enumerate(a) if c == ' ')
    if rule in ('header_name_truncated',):
        first = own_words[0].casefold()
        return not (len(own_words) >= 2 and first in _given_names() and first != other_words[0].casefold())
    if rule in ('header_name_is_address', 'header_name_is_misread_address', 'header_name_mangled_address'):
        keys = lambda ws: {family_ocr_key(w.casefold()) for w in ws if len(w) > 2}
        return bool(keys(own_words) & keys(other_words))
    if rule in ('header_name_ocr_skeleton', 'header_name_middle_dropped', 'header_name_short_first_name',
                'header_name_trailing_fragment', 'header_name_variant:ligature_loss',
                'header_name_variant:leading_name_ocr_spelling', 'header_name_variant:exact',
                'header_name_variant:given_name_variant', 'header_name_ocr_family:glued_tail',
                'header_name_ocr_family:boundary_junk', 'header_name_ocr_family:cut_word'):
        return True
    return False


@lru_cache(maxsize=1 << 17)
def same_family_person(a, b):
    """Reviewed names need the same given name and an OCR-equal *whole* surname."""
    a, b = family_name_words(a), family_name_words(b)
    def forms(words):
        orders = [words, words[::-1]] if len(words) == 2 else [words]
        return {(family_ocr_key(w[0]), family_ocr_key(''.join(rest))) for w in orders if len(w) >= 2
                for rest in (w[1:], tuple(p for p in w[1:] if len(p) > 1)) if rest}
    return bool(forms(a) & forms(b))


EXTRA_NICKNAMES = {'josh': 'joshua', 'greg': 'gregory', 'ben': 'benjamin', 'marty': 'martin', 'tommy': 'thomas',
                   'nick': 'nicholas', 'matt': 'matthew', 'sam': 'samuel', 'tim': 'timothy', 'tony': 'anthony',
                   'pete': 'peter', 'ron': 'ronald', 'don': 'donald', 'doug': 'douglas', 'chris': 'christopher',
                   'becky': 'rebecca', 'art': 'arthur', 'hank': 'henry', 'jake': 'jacob', 'fred': 'frederick',
                   'vince': 'vincent', 'zach': 'zachary', 'nate': 'nathan', 'jen': 'jennifer', 'jenny': 'jennifer',
                   'katie': 'katherine', 'mick': 'michael', 'mickey': 'michael', 'bobby': 'robert', 'billy': 'william',
                   'joey': 'joseph', 'danny': 'daniel', 'johnny': 'john', 'jack': 'john', 'sandy': 'alexander'}
NOISE_PREFIX = re.compile(r'^(?:on behalf of|imailto|mailto|hello|hey|hi|dear|good (?:morning|afternoon|evening)|bonjour|see|attn|attention|cc|bcc)\b[\s:,.-]*', re.I)


ALL_NICKNAMES = {**NICKNAMES, **EXTRA_NICKNAMES}


def nickname_root(word):
    # Built once: copying the table per call was 45% of build_communications.
    return ALL_NICKNAMES.get(word, word)


def header_name_words(label):
    """Periods separate initials; apostrophes do not split surnames."""
    text = unicodedata.normalize('NFKD', label or '').encode('ascii', 'ignore').decode().casefold()
    return tuple(w for w in re.findall(r'[a-z0-9]+', text.replace("'", '')) if w not in HONORIFICS)


def one_ocr_edit(a, b):
    if edit_distance(a, b, 1) != 1:
        return False
    if len(a) != len(b):
        return True
    x, y = next((x, y) for x, y in zip(a, b) if x != y)
    return any(x in group and y in group for group in ('il1', 'o0', 'uvy', 'mn', 'bd', 'ce', 'fr', 'gq', 'zs'))


class HeaderNameVariants:
    """Structural matches to frozen clean references, with no frequency tie-breaks
    between distinct people. Used by resolution and the private snapshot report.
    """
    def __init__(self, names, counts, references, aliases=()):
        self.names, self.counts = names, counts
        self.words = {i: header_name_words(s) for i, s in names.items()}
        self.refs = {i: self.words[i] for i in references if i not in aliases}
        self.exact, self.endpoints, self.glue = defaultdict(set), defaultdict(set), defaultdict(lambda: defaultdict(set))
        self.by_first = defaultdict(set)
        self.by_last = defaultdict(lambda: defaultdict(set))
        for i, words in self.refs.items():
            if not 2 <= len(words) <= 4 or any(w in GENERATIONAL | CREDENTIALS for w in words):
                continue
            self.exact[tuple(sorted(words))].add(i)
            self.endpoints[words[0], words[-1]].add(i)
            self.by_first[words[0]].add(words[-1])
            if len(words) == 2:
                self.by_last[words[-1]][words[0]].add(i)
            # Joining any two tokens and ignoring order also covers surname-first
            # headers with a middle name. Store only the joined word by remaining bag.
            for a, b in itertools.permutations(range(len(words)), 2):
                if len(words[a] + words[b]) >= 8:
                    rest = tuple(sorted(w for n, w in enumerate(words) if n not in (a, b)))
                    self.glue[rest][words[a] + words[b]].add(i)
        self.cache = {}

    @staticmethod
    def middle_compatible(a, b):
        return (not a or not b or len(a) == len(b)
                and all(x == y or min(len(x), len(y)) == 1 and x[0] == y[0] for x, y in zip(a, b)))

    def match(self, identity):
        if identity in self.cache:
            return self.cache[identity]
        words = self.words[identity]
        hits = {}
        if (identity.startswith('kg:') or not words or '?' in self.names[identity]
                or any(w in GENERATIONAL for w in words) or couple_words(list(words))):
            return None
        def add(ids, reason, score):
            for i in ids:
                if i != identity and (i not in hits or score < hits[i][0]):
                    hits[i] = (score, reason)
        add(self.exact.get(tuple(sorted(words)), ()), 'word_order', 0)
        for n in range(len(words) - 1):
            if any(len(w) == 1 and re.search(r'\b' + re.escape(w) + r'\.', self.names[identity], re.I)
                   for w in words[n:n + 2]):
                continue  # A printed middle initial is not a gap in a surname.
            joined = words[:n] + (words[n] + words[n + 1],) + words[n + 2:]
            if len(joined) >= 2:
                add(self.exact.get(tuple(sorted(joined)), ()), 'glued_tokens', 0)
        # Short boundary fragments only; preserve particles and real short surnames.
        junk = lambda w: len(w) <= 2 and w not in NAME_PARTICLES | GENERATIONAL | CREDENTIALS | SEPARATE_TAIL_WORDS
        for left, right in ((1, 0), (0, 1), (1, 1)):
            core = words[left:len(words) - right if right else None]
            if len(core) >= 2 and (not left or junk(words[0])) and (not right or junk(words[-1])):
                add(self.exact.get(tuple(sorted(core)), ()), 'boundary_junk', 1)
        # A greeting, "on behalf of" or mailto stuck to a name, then the bare name.
        stripped = NOISE_PREFIX.sub('', self.names[identity], count=1)
        if stripped != self.names[identity]:
            core = header_name_words(stripped)
            if len(core) >= 2:
                add(self.exact.get(tuple(sorted(core)), ()), 'noise_prefix', 1)
        if len(words) == 2 and not any(len(w) == 1 for w in words):
            first, last = words
            dominant = lambda ids: [i for i in ids if i.startswith('kg:') or self.counts[i] >= 5 * max(self.counts[identity], 1)]
            if True:
                # Nickname of the graph's spelling ("Josh Harris" / "Joshua Hdorpa"), same surname.
                root = nickname_root(first)
                for other, ids in self.by_last.get(last, {}).items():
                    if other != first and nickname_root(other) == root:
                        add(dominant(ids), 'nickname', 2)
                # One trailing letter lost from either word ("Hkewaw Rlopa", "Csog Lcorfota").
                for other, ids in self.by_last.get(last, {}).items():
                    if len(first) >= 4 and len(other) == len(first) + 1 and other.startswith(first):
                        add(dominant(ids), 'cut_word', 3)
                # A surname other people carry (Hodge/Hodges, Levin/Levine) is no cut-off word.
                attested = any(i != identity and (i.startswith('kg:') or self.counts[i] >= MIN_REFERENCE_NAME)
                               for ids in self.by_last.get(last, {}).values() for i in ids)
                for other in self.by_first.get(first, ()):
                    if not attested and len(last) >= 4 and len(other) == len(last) + 1 and other.startswith(last):
                        add(dominant(self.endpoints[first, other]), 'cut_word', 3)
        if 2 <= len(words) <= 4:
            for n in range(len(words)):
                form = words[n:] + words[:n]  # Last First Middle -> First Middle Last
                targets = self.endpoints.get((form[0], form[-1]), ())
                # A missing middle cannot choose between two graph namesakes.
                graph_middles = {self.refs[i][1:-1] for i in targets if i.startswith('kg:')}
                if len(graph_middles) > 1:
                    continue
                for i in targets:
                    other = self.refs[i]
                    middle = set(form[1:-1]) | set(other[1:-1])
                    forbidden = FUNCTION_WORDS | NON_NAME_WORDS | COMPANY_WORDS | ROLE_TOKENS | SEPARATE_TAIL_WORDS
                    dictionary, proper, _ = dictionary_words()
                    if (not middle & forbidden
                            and not any(w in dictionary and w not in proper and w not in NAME_PARTICLES and len(w) > 1 for w in middle)
                            and self.middle_compatible(form[1:-1], other[1:-1])):
                        add((i,), 'middle_name', 2)
            # Exact given name, a long surname, one insertion/deletion or visual
            # substitution. Any second surname within TWO edits vetoes the rule.
            near = defaultdict(set)
            for last in self.by_first.get(words[0], ()):
                targets = self.endpoints[words[0], last]
                if min(len(last), len(words[-1])) >= 6 and edit_distance(words[-1], last, 2) <= 2:
                    for i in targets:
                        if i != identity:
                            near[last].add(i)
            if len(near) == 1:
                last, targets = next(iter(near.items()))
                if one_ocr_edit(words[-1], last):
                    add((i for i in targets if self.middle_compatible(words[1:-1], self.refs[i][1:-1])
                         and (i.startswith('kg:') or identity not in self.refs
                              or self.counts[i] >= 5 * max(self.counts[identity], 1))),
                        'surname_one_ocr_edit', 3)
        for n, word in enumerate(words):
            choices = self.glue.get(tuple(sorted(words[:n] + words[n + 1:])), {})
            if len(word) >= 7:
                for glued, distance, _ in process.extract(word, choices.keys(), scorer=Levenshtein.distance, score_cutoff=1, limit=None):
                    add(choices[glued], 'glued_tokens', 3 + distance)
        result = None
        if hits:
            graph_hits = {i for i in hits if i.startswith('kg:')}
            score = min(hits[i][0] for i in graph_hits) if graph_hits else min(s for s, _ in hits.values())
            best = {i for i, (s, _) in hits.items() if s == score}
            if graph_hits:
                best &= graph_hits
            # Compatible spellings of ONE reference may share a canonical. Never
            # choose between graph people, even with identical words.
            kg = {i for i in best if i.startswith('kg:')}
            signatures = {tuple(sorted(self.refs[i])) for i in best}
            if len(kg) == 1 or not kg and len(signatures) == 1:
                target = next(iter(kg)) if kg else max(best, key=lambda i: (self.counts[i], i))
                if (identity not in self.refs or target.startswith('kg:')
                        or self.counts[target] > self.counts[identity]):
                    result = (target, hits[target][1])
        self.cache[identity] = result
        return result

    def surname_rivals(self, identity, destination):
        own, other = self.words[identity], self.words[destination]
        if (len(own) < 2 or len(other) < 2 or own[0] != other[0]
                or ''.join(own[1:]) == ''.join(other[1:])
                or min(len(own[-1]), len(other[-1])) < 6 or not one_ocr_edit(own[-1], other[-1])):
            return False
        return any(last != other[-1] and edit_distance(own[-1], last, 2) <= 2
                   and self.endpoints[own[0], last] - {identity}
                   for last in self.by_first.get(own[0], ()))


class FrequentNameFamilies:
    """Bounded first-name buckets, not an all-pairs matrix or an evolving fuzzy chain.

    References and scores are frozen before merging. An exact spelling beats a
    one-edit spelling, but frequency never breaks an equal-distance tie. Initials
    only expand against an exact surname. Full middle names remain significant.
    """
    def __init__(self, names, counts, reviewed, db=None, spellings=None):
        self.names, self.counts = names, counts
        self.spellings = spellings or {}
        self.reviewed = reviewed
        self.words = {i: family_name_words(label) for i, label in names.items()}
        self.references = {}
        self.by_first, self.by_initial = defaultdict(set), defaultdict(set)
        forbidden = (FUNCTION_WORDS | NON_NAME_WORDS | ROLE_TOKENS | COMPANY_WORDS
                     | SEPARATE_TAIL_WORDS | GENERATIONAL | {'jr', 'sr'})
        for identity, words in self.words.items():
            verdict = reviewed.get(identity, {}).get('verdict')
            if ((counts[identity] < MIN_REFERENCE_NAME and not identity.startswith('kg:')) or not 2 <= len(words) <= 4
                    or len(words[0]) < 2 or len(words[-1]) < 3
                    or any(len(w) > 25 for w in words)
                    or re.search(r'@|[a-z]{2,}\.[a-z]{2,}', names[identity], re.I)
                    or any(w in forbidden for w in words if len(w) > 1)
                    or verdict not in (None, 'ok_person', 'variant_of_known', 'fix_label')
                    or label_quality(names[identity]) != 'ok' or plain_text(names[identity])):
                continue
            # Internal initials are optional, but full middle names / surname particles are not.
            surname = ''.join(w for w in words[1:] if len(w) > 1)
            self.references[identity] = (family_ocr_key(words[0]), family_ocr_key(surname))
            self.by_first[family_ocr_key(words[0])].add(identity)
            self.by_initial[words[0][0]].add(identity)
        self.reference_counts = Counter()
        for identity, key in self.references.items():
            self.reference_counts[key] += counts[identity]
        self.vocabulary = {w for i in self.references for w in self.words[i] if len(w) > 1}
        self.vocabulary.update(w for i, words in self.words.items()
                               if i.startswith('kg:') or reviewed.get(i, {}).get('verdict') == 'ok_person'
                               for w in words if len(w) > 1)
        self.tail_excluded = (self.vocabulary | SEPARATE_TAIL_WORDS | GENERATIONAL | CREDENTIALS
                              | (FUNCTION_WORDS - {'a'}) | (NON_NAME_WORDS - {'cc', 'bcc'}))
        self.cache = {}
        self.incomplete = set()
        # A surname in the remainder means two people, including when OCR has
        # removed every separator. Do not let frequent junk tails become surnames.
        trusted_words = {}
        reference_keys = set(self.references.values())
        for identity, words in self.words.items():
            if not (identity.startswith('kg:') or reviewed.get(identity, {}).get('verdict') == 'ok_person'):
                continue
            clean = words
            # A person verdict attests the person, not a bracket fragment in
            # their label. Such a fragment must not become a global surname veto.
            for spelling in self.spellings.get(identity) or (names[identity],):
                spelling = spelling[:1].upper() + spelling[1:]
                stripped = family_name_words(strip_trailing_fragments(spelling))
                if (len(stripped) >= 2 and len(stripped) < len(clean)
                        and (len(stripped) >= 3 or
                             (family_ocr_key(stripped[0]), family_ocr_key(''.join(w for w in stripped[1:] if len(w) > 1)))
                             in reference_keys)):
                    clean = stripped
            trusted_words[identity] = clean
        self.known_surnames = {family_ocr_key(words[-1]) for words in trusted_words.values()
                               if len(words) >= 2 and len(words[-1]) >= 2}
        self.trusted_surnames = set(self.known_surnames)
        self.trusted_surname_pattern = re.compile('|'.join(sorted((s for s in self.trusted_surnames
            if len(s) >= 4 and re.search(r'[^lrn]', s)), key=lambda s: (-len(s), s))) or r'(?!)')
        self.surname_firsts = defaultdict(set)
        self.header_junk = {family_ocr_key(w) for w in ('com', 'co', 'corn', 'mail', 'gmail', 'email', 'mailto', 'imailto')}
        known_firsts = {self.references[i][0] for i in self.references if i.startswith('kg:')
                        or reviewed.get(i, {}).get('verdict') == 'ok_person'}
        self.full_names = {first + last for first, last in self.references.values()
                           if first in known_firsts and last not in self.header_junk}
        self.full_name_pattern = re.compile('|'.join(sorted(self.full_names, key=lambda s: (-len(s), s))) or r'(?!)')
        self.surname_pattern = re.compile('|'.join(sorted((s for s in self.known_surnames if len(s) >= 3),
                                                        key=lambda s: (-len(s), s))) or r'(?!)')
        self.support = defaultdict(set)
        self.addresses = defaultdict(Counter)
        if db is not None:
            self.load_support(db)
        self.aliases = self.reference_aliases()
        for i, (first, surname) in self.references.items():
            if (i not in self.aliases and len(self.words[i]) == 2 and first not in self.header_junk
                    and surname not in self.header_junk and re.search(r'[^lrn]', surname)):
                self.surname_firsts[surname].add(first)
        self.known_surnames.update(self.surname_firsts)
        self.surname_pattern = re.compile('|'.join(sorted((s for s in self.known_surnames if len(s) >= 3),
                                                        key=lambda s: (-len(s), s))) or r'(?!)')
        # Recheck against the final frozen surname vocabulary, not tailed names.
        self.aliases = self.reference_aliases()
        self.header_variants = HeaderNameVariants(names, counts, self.references, self.aliases)
        structural = {i: result for i in self.references if not i.startswith('kg:')
                      and (result := self.header_variants.match(i)) is not None}
        self.aliases.update(structural)
        for i, (target, kind) in list(self.aliases.items()):
            seen = {i}
            while target in self.aliases and target not in seen:
                seen.add(target)
                target = self.aliases[target][0]
            if target not in seen:
                self.aliases[i] = (target, kind)

    def load_support(self, db):
        """One streaming scan, only retaining context for possible truncations/refs."""
        needed = set(self.references)
        for identity, words in self.words.items():
            if len(words) >= 2:
                keys = {family_ocr_key(''.join(rest)) for rest in
                        (words[1:], tuple(w for w in words[1:] if len(w) > 1)) if rest}
                if any(last.startswith(key) and len(key) >= 3
                       or min(len(key), len(last)) >= 4 and Levenshtein.distance(key, last, score_cutoff=2) <= 2
                       for i in self.by_first.get(family_ocr_key(words[0]), ())
                       for _, last in (self.references[i],) for key in keys):
                    needed.add(identity)
        document = 'efta_number' if any(r[1] == 'efta_number' for r in db.execute('PRAGMA table_info(evidence)')) else 'NULL'
        for source, target, doc, kind in db.execute(f'SELECT source_id,target_id,{document},relationship_type FROM evidence'):
            if kind in ('header_label_association', 'header_name_association', 'upstream_resolved_header_identity'):
                self.addresses[source][target] += 1
            if source in needed:
                self.support[source].add(target)
                if doc:
                    self.support[source].add('doc:' + doc)
            if target in needed:
                self.support[target].add(source)
                if doc:
                    self.support[target].add('doc:' + doc)
        # A KG person may have no raw evidence: exact header spellings attest it.
        grouped = defaultdict(set)
        grouped_addresses = defaultdict(Counter)
        for identity, key in self.references.items():
            grouped[key].update(self.support[identity])
            grouped_addresses[key].update(self.addresses[identity])
        for identity, key in self.references.items():
            self.support[identity] = grouped[key]
            self.addresses[identity] = grouped_addresses[key]

    def shared_context(self, member, target):
        return not self.support[member].isdisjoint(self.support[target])

    def spelling_context(self, member, target):
        """Different attested address sets outweigh a shared correspondent/page."""
        own, other = self.addresses[member], self.addresses[target]
        if sum(own.values()) >= 3 and sum(other.values()) >= 3:
            return not own.keys().isdisjoint(other)
        return self.shared_context(member, target)

    def anchored_name(self, member, target):
        if not target.startswith('kg:'):
            return False
        words = self.words[member]
        first, surname = self.references[target]
        if (not words or family_ocr_key(words[0]) != first or len(words) < 2
                or any(w in SEPARATE_TAIL_WORDS | GENERATIONAL | CREDENTIALS for w in words[1:])):
            return False
        # Keep a real alternative whole surname. A single unpunctuated letter
        # inside a split surname must also be tried literally, not discarded as
        # a middle initial (a dotted middle initial remains significant).
        own_surname = family_ocr_key(words[-1])
        alternative_surname = own_surname in self.trusted_surnames and own_surname != surname
        if alternative_surname and (len(words) == 2 or not any(len(w) == 1 for w in words[1:-1])
                or any(re.search(r'\b[a-z]\.', spelling, re.I) for spelling in
                       self.spellings.get(member) or (self.names[member],))):
            return False
        keys = {family_ocr_key(''.join(rest)) for rest in
                (words[1:], tuple(w for w in words[1:] if len(w) > 1)) if rest}
        if alternative_surname:
            keys = {family_ocr_key(''.join(words[1:]))}
        distance = min((Levenshtein.distance(key, surname, score_cutoff=2)
                        for key in keys if min(len(key), len(surname)) >= 4), default=3)
        kind = 'anchored_spelling'
        if distance > 2:
            literal = ''.join(words)
            reference = ''.join(self.words[target])
            # The legacy matcher already recognizes these dropped ligatures,
            # but a frequent damaged reference needs the same evidence checks
            # before it can stop competing with the complete KG spelling.
            if (len(''.join(words[1:])) < 3 or len(literal) < 0.65 * len(reference)
                    or not any(is_subsequence(literal, form) for form in
                               (reference, re.sub(r'in|rn|ni', 'm', reference)))):
                return False
            kind = 'anchored_ligature'
        correct_count = self.reference_counts[first, surname]
        return (kind if correct_count >= max(MIN_REFERENCE_NAME, 5 * self.counts[member])
                and self.spelling_context(member, target) else None)

    def junk_tail(self, tail, target, member=None):
        return self.tail_veto(tail, target, member) is None

    def tail_veto(self, tail, target, member=None):
        """Explain a tail rejection without exposing labels in logs."""
        if not tail:
            return 'empty_tail'
        words = family_name_words(tail)
        fragment = False
        odd_company_bracket = False
        if len(words) == 1:
            for spelling in self.spellings.get(member) or (self.names.get(member, ''),):
                spelling = spelling[:1].upper() + spelling[1:]
                tokens = spelling.split()
                if tokens and family_name_words(tokens[-1]) == words:
                    fragment |= strip_trailing_fragments(spelling) != spelling
                    odd_company_bracket |= words == ('co',) and bool(re.fullmatch(r'c[O0]', tokens[-1]))
        if any(w in (SEPARATE_TAIL_WORDS - ({'co'} if odd_company_bracket else set())) | GENERATIONAL | CREDENTIALS for w in words):
            return 'role_legal_or_generational_tail'
        if words and all(w in {'mailto', 'imailto', 'cc', 'bcc'} for w in words):
            return None
        key = family_ocr_key(''.join(words))
        if (len(words) == 1 and 5 <= len(words[0]) <= 9 and words[0].endswith('to')
                and key not in self.trusted_surnames
                and min(Levenshtein.distance(key, family_ocr_key(marker), score_cutoff=2)
                        for marker in ('mailto', 'imailto')) <= 2):
            return None  # OCR of the mailto delimiter, not a surname inside it
        own = ''.join(self.references[target])
        if any(m[0] != own for m in self.full_name_pattern.finditer(key)):
            return 'second_full_name_in_tail'
        # A repeated name/address is harmless; another known surname is not.
        surname = self.references[target][1]
        own_names = {surname, self.references[target][0]}
        def other_person(s):
            return s in self.trusted_surnames or bool(self.surname_firsts[s] - {self.references[target][0]})
        if any(m[0] not in own_names for m in self.trusted_surname_pattern.finditer(key)):
            return 'trusted_surname_substring_in_tail'
        if (any(m[0] not in own_names and other_person(m[0]) and key.endswith(m[0])
                        and not (fragment and (m[0] not in self.trusted_surnames
                                 or len(key) <= 4 and m.span() != (0, len(key))))
                        and re.search(r'[^lrn]', m[0]) for m in self.surname_pattern.finditer(key))
                    or any((s := family_ocr_key(w)) not in own_names and s in self.known_surnames
                           and not (fragment and s not in self.trusted_surnames)
                           and other_person(s) for w in words)):
            return 'second_surname_in_tail'
        return None

    def tail_score(self, words, target, member=None):
        if target in self.incomplete:
            return None  # an unresolved surname prefix is not a complete name
        for tail, kind in self.prefix_tails(words, target):
            description = family_name_words(tail)
            if description == ('office',) or (description[:2] == ('friend', 'of') and len(description) > 2):
                return (1, 'self_description')
            if self.junk_tail(tail, target, member):
                return (1, kind)
        return None

    def literal_reference(self, target):
        """A validated surname gap is literal evidence, not an optional initial."""
        if (getattr(self, 'aliases', {}).get(target, (None, None))[1]
                in ('anchored_spelling', 'anchored_ligature')
                and any(len(w) == 1 for w in self.words[target][1:-1])):
            return family_ocr_key(''.join(self.words[target][1:]))
        return self.references[target][1]

    def prefix_tails(self, words, target):
        """Literal remainder after a full name, preserving tail word boundaries."""
        if not words:
            return
        first, surname = self.references[target][0], self.literal_reference(target)
        if family_ocr_key(words[0]) != first:
            return
        # Keep tail word boundaries: joining "Smith Jr" into "smithjr" would
        # evade the second-person and generational-suffix vetoes.
        for rest in (words[1:], tuple(w for w in words[1:] if len(w) > 1)):
            literal = ''.join(rest)
            if not family_ocr_key(literal).startswith(surname):
                continue
            end = next((n for n in range(1, len(literal) + 1)
                        if family_ocr_key(literal[:n]) == surname), None)
            if end is None:
                continue
            offset = end
            for n, word in enumerate(rest):
                if offset >= len(word):
                    offset -= len(word)
                    continue
                tail = ' '.join((word[offset:], *rest[n + 1:]))
                yield tail, 'glued_tail' if offset else 'long_tail'
                break

    def reference_aliases(self):
        """Freeze clean references; frequent junk must not compete with its name."""
        aliases = {}
        def protected(identity):
            return identity.startswith('kg:') or self.reviewed.get(identity, {}).get('verdict') == 'ok_person'
        def root(identity):
            while identity in aliases:
                identity = aliases[identity][0]
            return identity
        def choose(targets):
            # Optional middle initials do not distinguish an uninitialled label
            # from one KG person, but two KG people remain ambiguous.
            keys = {self.references[i] for i in targets}
            kg = {i for i in targets if i.startswith('kg:')}
            if len(keys) == 1 and len(kg) <= 1:
                return max(targets, key=lambda i: (i.startswith('kg:'), self.counts[i], i))
        # Establish complete KG spellings before junk or truncated references can
        # compete with them. Other completed surnames still veto a prefix match.
        for identity, (first, surname) in self.references.items():
            if protected(identity):
                continue
            kg = {i for i in self.by_first[first] if i.startswith('kg:')}
            targets = {}
            for target in kg:
                other = self.references[target][1]
                correct_count = sum(self.counts[i] for i in self.by_first[first]
                                    if self.references[i][1] == other)
                tail = self.tail_score(self.words[identity], target, identity)
                own_initials = tuple(w for w in self.words[identity][1:-1] if len(w) == 1)
                target_initials = tuple(w for w in self.words[target][1:-1] if len(w) == 1)
                if surname == other and (not own_initials or not target_initials or own_initials == target_initials):
                    targets[target] = 'ocr_equal'
                elif tail:
                    targets[target] = tail[1]
                elif (surname != other and other.startswith(surname)
                      and len(''.join(w for w in self.words[identity][1:] if len(w) > 1)) >= 3
                      and correct_count >= MIN_REFERENCE_NAME and self.shared_context(identity, target)):
                    completions = {self.references[i][1] for i in self.by_first[first]
                                   if self.references[i][1].startswith(surname)}
                    if all(other.startswith(s) or s.startswith(other)
                           or Levenshtein.distance(s, other, score_cutoff=2) <= 2 for s in completions):
                        targets[target] = 'truncated_context'
                elif kind := self.anchored_name(identity, target):
                    targets[target] = kind
            if (target := choose(targets)) is not None:
                aliases[identity] = (target, targets[target])
        # f/r is a common terminal OCR substitution not covered by stroke keys.
        # Without a KG anchor require both frequency dominance and shared context.
        for identity, (first, surname) in self.references.items():
            if protected(identity) or identity in aliases:
                continue
            targets = set()
            for candidate in self.by_first[first] - {identity}:
                target = root(candidate)
                other = self.references[target][1]
                changes = [(a, b) for a, b in zip(surname, other) if a != b]
                if (len(surname) == len(other) and len(changes) == 1 and set(changes[0]) == {'f', 'r'}
                        and self.counts[target] >= 5 * max(self.counts[identity], 1)
                        and self.shared_context(identity, target)):
                    targets.add(target)
            if (target := choose(targets)) is not None:
                aliases[identity] = (target, 'dominant_ocr_spelling')
        # Short frequent prefixes are still fragments when there is exactly one
        # completion and shared evidence; classify them before considering tails.
        for identity in sorted(self.references, key=lambda i: -len(self.references[i][1])):
            if protected(identity) or identity in aliases:
                continue
            first, surname = self.references[identity]
            if not 3 <= len(''.join(w for w in self.words[identity][1:] if len(w) > 1)) <= 4:
                continue
            completions = {root(i) for i in self.by_first[first] if i != identity
                           and self.references[root(i)][1].startswith(surname)
                           and self.references[root(i)][1] != surname}
            if (target := choose(completions)) is not None and self.shared_context(identity, target):
                aliases[identity] = (target, 'truncated_context')
        self.incomplete = {i for i, (first, surname) in self.references.items()
                           if not protected(i) and i not in aliases
                           and 3 <= len(''.join(w for w in self.words[i][1:] if len(w) > 1)) <= 4
                           and any(self.references[root(j)][1] != surname
                                   and self.references[root(j)][1].startswith(surname)
                                   for j in self.by_first[first])}
        # Tails strictly shorten a surname key, so no cycles or evolving edit chains.
        for identity in sorted(self.references, key=lambda i: len(self.references[i][1])):
            if protected(identity) or identity in aliases:
                continue
            first, surname = self.references[identity]
            targets = {}
            for candidate in self.by_first[first] - {identity}:
                target = root(candidate)
                other = self.references[target][1]
                literal_surname = self.references[candidate][1]
                if (aliases.get(candidate, (None, None))[1] != 'truncated_context'
                        and len(literal_surname) < len(surname)
                        and (tail := self.tail_score(self.words[identity], candidate, identity))):
                    targets[target] = tail[1]
                elif kind := self.anchored_name(identity, target):
                    targets[target] = kind
            if targets:
                kg = {i for i in targets if i.startswith('kg:')}
                shortest = min(len(self.references[i][1]) for i in targets)
                best = kg or {i for i in targets if len(self.references[i][1]) == shortest}
                if (target := choose(best)) is not None:
                    aliases[identity] = (target, targets[target])
        # Longest references first, so Indyk and Ind resolve against the same
        # completed name. Never select the longest of *different* completions.
        for identity in sorted(self.references, key=lambda i: -len(self.references[i][1])):
            if protected(identity) or identity in aliases:
                continue
            first, surname = self.references[identity]
            literal = ''.join(w for w in self.words[identity][1:] if len(w) > 1)
            if len(literal) < 3:
                continue
            completions = {root(i) for i in self.by_first[first] if i != identity
                           and self.references[root(i)][1].startswith(surname)
                           and self.references[root(i)][1] != surname}
            if (target := choose(completions)) is None:
                continue
            correct_count = sum(self.counts[i] for i in self.by_first[first]
                                if self.references[i] == self.references[target])
            if (self.shared_context(identity, target)
                    and (len(literal) <= 4 or target.startswith('kg:') and correct_count >= MIN_REFERENCE_NAME)):
                aliases[identity] = (target, 'truncated_context')
        return {i: (root(target), kind) for i, (target, kind) in aliases.items()}

    def score(self, words, target, member=None, allow_no_context=False):
        first, surname = self.references[target][0], self.literal_reference(target)
        given = family_ocr_key(words[0])
        initial = len(words[0]) == 1 and self.words[target][0].startswith(words[0])
        if given != first and not initial:
            return None
        surname_end = next((n for n in range(1, len(words))
                            if family_ocr_key(''.join(w for w in words[1:n + 1] if len(w) > 1)).startswith(surname)), len(words) - 1)
        own_initials = tuple(family_ocr_key(w) for w in words[1:surname_end] if len(w) == 1)
        target_initials = tuple(family_ocr_key(w) for w in self.words[target][1:-1] if len(w) == 1)
        if own_initials and target_initials and own_initials != target_initials:
            return None
        if family_ocr_key(''.join(words[1:])) == surname:
            return (0, 'initial' if initial else 'ocr_equal')
        rest = tuple(w for w in words[1:] if len(w) > 1)
        if not rest:
            return None
        last = family_ocr_key(''.join(rest))
        if last == surname:
            return (0, 'initial' if initial else 'ocr_equal')
        if initial:
            return None  # never combine an initial expansion with a damaged surname
        tail_score = self.tail_score(words, target, member)
        if tail_score is not None:
            return tail_score
        if surname != self.references[target][1]:
            return None  # do not turn a repaired surname gap back into an initial
        if member is not None and (kind := self.anchored_name(member, target)):
            return (1, kind)
        # A full reference followed by a short header fragment. Known name words,
        # roles and suffixes cannot be junk (Mary Efomo Li, John Smith Jr).
        tail = words[-1]
        core = family_ocr_key(''.join(w for w in words[1:-1] if len(w) > 1))
        if core == surname and tail in self.tail_excluded:
            return None
        if core == surname and 1 <= len(tail) <= 4 and tail not in self.tail_excluded:
            return (1, 'short_tail')
        if surname.startswith(last):
            return ((1, 'truncated_context') if len(''.join(rest)) >= 3
                    and (allow_no_context or member is not None and self.shared_context(member, target)) else None)
        distance = Levenshtein.distance(last, surname, score_cutoff=2)
        reference = ''.join(w for w in self.words[target][1:] if len(w) > 1)
        if distance <= (2 if len(reference) >= 7 else 1) and min(len(last), len(surname)) >= 4:
            return (distance, 'surname_edits')
        # Long runs of bracket strokes can replace the end of a surname. Require
        # four literal leading letters and at most five lost letters, not arbitrary
        # subsequences (which can consume another person's surname).
        literal = ''.join(rest)
        strokes = re.fullmatch(r'([a-z]{4,}?)[il1]{3,}', literal)
        if (strokes and surname.startswith(prefix := family_ocr_key(strokes[1]))
                and len(surname) - len(prefix) <= 5 and self.counts[target] >= 10):
            return (2, 'stroke_suffix')
        # A repeated end of the surname followed by an OCR bracket, glued to it.
        # Other real surname extensions (Smithson, Goldberg) are not stripped.
        if literal.startswith(reference):
            tail = literal[len(reference):]
            if (re.fullmatch(r'[a-z]{0,3}[il1]', tail)
                    and Levenshtein.distance(tail[:-1], reference[-4:], score_cutoff=2) <= 2):
                return (2, 'glued_tail')
        return None

    def candidates(self, identity):
        if identity in self.cache:
            return self.cache[identity]
        words = self.words[identity]
        scored = {}
        literal_tails = {}
        if result := self.header_variants.match(identity):
            target, kind = result
            target = self.aliases.get(target, (target,))[0]
            self.cache[identity] = {target: (-1, kind)}
            return self.cache[identity]
        if identity in self.aliases:
            target, kind = self.aliases[identity]
            scored[target] = (1, kind)
        elif len(words) >= 2 and '?' not in self.names[identity] and not couple_words(list(words)):
            forms = [words]
            if len(words) == 2:  # unpunctuated "Surname First"
                forms.append(words[::-1])
            for form in forms:
                bucket = (self.by_initial.get(form[0], ()) if len(form[0]) == 1
                          else self.by_first.get(family_ocr_key(form[0]), ()))
                for target in bucket:
                    if target in self.aliases and self.aliases[target][1] == 'truncated_context':
                        continue
                    result = self.score(form, target, identity)
                    canonical = self.aliases.get(target, (target,))[0]
                    if result is not None and result[0] == 0 and target in self.aliases:
                        result = (0, self.aliases[target][1])
                    if result is not None and (canonical not in scored or result < scored[canonical]):
                        scored[canonical] = result
                    if result is not None and result[1] in ('short_tail', 'long_tail', 'glued_tail'):
                        literal = family_ocr_key(''.join(form[1:]))
                        reference = family_ocr_key(''.join(self.words[target][1:]))
                        if literal.startswith(reference) and len(literal) > len(reference):
                            literal_tails[canonical] = max(literal_tails.get(canonical, 0), len(reference))
            if any(kind == 'truncated_context' for _, kind in scored.values()):
                possible = set()
                for form in forms:
                    for target in self.by_first.get(family_ocr_key(form[0]), ()):
                        result = self.score(form, target, identity, allow_no_context=True)
                        if result is not None and result[1] == 'truncated_context':
                            possible.add(self.aliases.get(target, (target,))[0])
                keys = {(self.references[i], tuple(w for w in self.words[i][1:-1] if len(w) == 1)) for i in possible}
                if len(keys) > 1:
                    # Context confirms a unique completion; it must not choose
                    # between different complete names sharing the prefix.
                    for target in possible:
                        scored.setdefault(target, (1, 'truncated_context'))
        # "Epstein ..." is a more complete literal name than "Eps ...". A
        # frequent truncated reference must not tie with the full name's tail.
        tailed = {i for i, (_, kind) in scored.items() if kind in ('long_tail', 'glued_tail')}
        shorter = {i for i in tailed if any(self.references[j][0] == self.references[i][0]
                   and self.references[j][1] != self.references[i][1]
                   and self.references[j][1].startswith(self.references[i][1]) for j in tailed)}
        scored = {i: s for i, s in scored.items() if i not in shorter}
        if literal_tails and sum(i.startswith('kg:') for i in scored) < 2:
            longest = max(literal_tails.values())
            scored = {i: s for i, s in scored.items() if s[1] not in ('short_tail', 'long_tail', 'glued_tail')
                      or literal_tails.get(i, 0) == longest}
        self.cache[identity] = scored
        return scored


def resolve_identities(db, email_values, curated, never=(), participant_labels=None, matcher=None, entity_names=None, person_names=None,
                       spellings=None, families=None, diagnostics=None, support_aliases=None, context_corpus=False):
    """Map pseudonymous endpoints onto one identity: {member: (canonical, rule, confidence)}.

    1. Reviewed merges from config/identity_merges.json.
       Frozen frequent-name families then resolve OCR spellings, gaps, truncation,
       tails and initials to a unique nearest reference. Reviewed people,
       distinct frequent full names and transitive separation vetoes are protected.
    2. OCR variants (raw values are only in memory): addresses identical after
       ocr_repair merge into the most frequent one; otherwise a local part within
       edit distance 1 (length >= 6) or 2 (length >= 8), same repaired domain, of
       an address with >= 10x as many records. Variants listed under ocr_merges
       in config/node_review.json still merge, with confidence needs_review,
       unless their review says same.
    3. An address whose header display names point to one identity (a graph
       person, or a header-name participant of two or more words) in >= 90% and
       >= 3 of its labelled records merges into it. Shared or assistant-run
       mailboxes are possible, so every such merge is recorded for review.
    """
    def identity_id(value):
        return f'kg:{value}' if isinstance(value, int) else value

    blocked = {frozenset(identity_id(value) for value in pair) for pair in never}
    if any(len(pair) != 2 for pair in blocked):
        raise ValueError('never_merge entries must contain two distinct identities')
    review = node_review()
    reviewed = review.get('participants', {})
    flagged = {k for k, item in review.get('ocr_merges', {}).items() if item.get('review') != 'same'}
    names = {**(person_names or {}), **(participant_labels or {})}
    compound_ids = set(resolved_header_compounds(names, lambda i: i))
    label_counts = Counter()
    for column in ('source_id', 'target_id'):
        label_counts.update(dict(db.execute(f"SELECT {column}, count(*) FROM evidence WHERE {column} LIKE 'participant_%' OR {column} LIKE 'kg:%' GROUP BY {column}")))
    families = families or FrequentNameFamilies(names, label_counts, reviewed, db, spellings)
    reference_anchors = set()
    def rejected(member, target, rule, reason, identity=None):
        if diagnostics is not None:
            diagnostics.append(dict(member=member, target=target, rule=rule,
                                    reason=reason, blocking_member=identity or member))
    # Like luna_review.apply, any high-confidence 'different' is a veto. Old
    # person-only reviews of companies are excluded there; here only known name
    # and mailbox endpoints are relevant. Reviewer prose stays private.
    for path in sorted((ROOT / 'data/private/luna_review').glob('merges_*.json')):
        for key, answer in json.loads(path.read_text()).items():
            if answer.get('verdict') == 'different' and answer.get('confidence') == 'high':
                pair = [f'kg:{s}' if s.isdigit() else s for s in key.split('|')]
                if (len(pair) == 2 and all(i in names or i in email_values for i in pair)
                        and any(i in (participant_labels or {}) or i in email_values for i in pair)):
                    if any(member in names and target in families.references
                           and (score := families.tail_score(families.words[member], target, member)) is not None
                           and score[1] == 'self_description' for member, target in (pair, pair[::-1])):
                        rejected(pair[0], pair[1], 'imported_review', 'self_description_review_not_person_separation')
                        continue
                    blocked.add(frozenset(pair))
    blocked_by_identity = defaultdict(set)
    for pair in blocked:
        for identity in pair:
            blocked_by_identity[identity].add(pair)
    def final(identity, seen=()):
        target = mapping.get(identity, (identity,))[0]
        return identity if target == identity or target in seen else final(target, (*seen, identity))

    def address_short_name(identity, destination):
        own, other = families.words.get(identity, ()), families.words.get(destination, ())
        return (len(own) == 2 and len(other) >= 2 and len(own[1]) <= 3
                and family_ocr_key(own[0]) == family_ocr_key(other[0])
                and family_ocr_key(other[-1]).startswith(family_ocr_key(own[1])))

    def dotted_surname_initial(identity):
        words = families.words.get(identity, ())
        return (len(words) == 2 and len(words[1]) == 1
                and any(re.search(r'\b[a-z]\.', spelling, re.I) for spelling in
                        (spellings or {}).get(identity) or (names.get(identity, ''),)))

    validated_initials = defaultdict(set)
    context_confirmed = {}
    def address_initial_tail(identity, destination):
        words = families.words.get(identity, ())
        return (len(words) >= 3 and len(words[1]) == 1
                and destination in validated_initials[(family_ocr_key(words[0]), words[1])]
                and families.junk_tail(' '.join(words[2:]), destination, identity))

    class GuardedMerges(dict):
        """All merge paths must respect separations between entire clusters.

        Check the prospective mapping, including reviewed links and later
        retargeting of canonical nodes. Never export a forbidden transitive link.
        """
        def __init__(self):
            super().__init__()
            self.incoming = defaultdict(set)

        def descendants(self, member):
            found, pending = set(), [member]
            while pending:
                identity = pending.pop()
                if identity not in found:
                    found.add(identity)
                    pending.extend(self.incoming.get(identity, ()))
            return found

        def __delitem__(self, member):
            self.incoming[self[member][0]].discard(member)
            super().__delitem__(member)

        def __setitem__(self, member, value):
            missing = object()
            previous = self.get(member, missing)
            moved = self.descendants(member)
            destination = final(value[0])
            # A greeting/tag prefix rule moves exactly such labels; its destination must be clean.
            checked = {destination} if value[1] == 'header_name_head_noise' else moved | {destination}
            if (value[1] not in ('reviewed_identity_merge', 'reviewed_audit_merge')
                    and any(header_junk_reason(names[i]) for i in checked & names.keys())):
                rejected(member, destination, value[1], 'non_name_header_syntax')
                return
            if (value[1] not in ('reviewed_identity_merge', 'reviewed_audit_merge')
                    and moved & compound_ids and destination not in compound_ids):
                rejected(member, destination, value[1], 'compound_name_not_single_person')
                return
            if value[1] not in ('reviewed_identity_merge', 'reviewed_audit_merge') and any(
                    non_mailbox_reason(email_values.get(i, '')) for i in moved | {destination}):
                rejected(member, destination, value[1], 'transport_identifier_not_mailbox')
                return
            if value[1] not in ('reviewed_identity_merge', 'reviewed_audit_merge') and destination in names:
                evidence_short = (value[1] in ('header_name_on_one_identity_addresses', 'short_header_name_on_one_identity')
                                  and address_short_name(member, destination))
                evidence_short |= (value[1].startswith('header_context:') and context_confirmed.get(member) == destination)
                reviewed_initial = (dotted_surname_initial(member) and any(
                    families.words.get(alias) == families.words[member] and final(alias) == destination
                    for alias in curated if alias != member))
                evidence_short |= reviewed_initial
                if dotted_surname_initial(member) and not evidence_short:
                    rejected(member, destination, value[1], 'initial_requires_address_evidence')
                    return
                for identity in sorted(moved & names.keys()):
                    context_alias = (value[1].startswith('header_context:')
                                     and context_confirmed.get(identity) == destination)
                    own_words = families.words[identity]
                    if not context_alias and families.header_variants.surname_rivals(identity, destination):
                        rejected(member, destination, value[1], 'second_surname_within_two_edits', identity)
                        return
                    variant = families.header_variants.match(identity)
                    structural_alias = (variant is not None and variant[0] in reference_roots
                                        and final(reference_roots[variant[0]]) == destination)
                    own_surname = family_ocr_key(own_words[-1]) if len(own_words) == 2 else None
                    if (not context_alias and own_surname in families.trusted_surnames
                            and destination in families.references
                            and own_surname != families.references[destination][1]
                            and not (structural_alias and variant[1] != 'surname_one_ocr_edit')
                            and not same_family_person(names[identity], names[destination])):
                        rejected(member, destination, value[1], 'different_attested_surname', identity)
                        return
                    address_tail = (value[1] == 'header_name_address_tail'
                                    and (identity == member or same_family_person(names[identity], names[member])
                                         or member in families.references
                                         and families.tail_score(families.words[identity], member, identity) is not None))
                    structured = (families.score(families.words[identity], destination, identity)
                                  if identity.startswith('participant_') and destination in families.references
                                  and value[1].startswith('header_name_ocr_family:') else None)
                    initial_tail = (value[1] == 'header_name_address_initial_tail'
                                    and address_initial_tail(identity, destination))
                    structured_kinds = {'ocr_equal', 'initial', 'long_tail', 'short_tail', 'glued_tail',
                                        'truncated_context', 'anchored_spelling', 'anchored_ligature', 'self_description'}
                    literal_alias = (value[1].startswith('header_name_ocr_family:') and any(
                        alias in families.aliases and final(reference_roots[families.aliases[alias][0]]) == destination
                        and (score := families.score(families.words[identity], alias, identity)) is not None
                        and score[1] in structured_kinds
                        for alias in families.by_first.get(family_ocr_key(families.words[identity][0]), ())
                        ) if families.words[identity] else False)
                    family_alias = (context_alias or structural_alias or value[1] in ADDRESS_LABEL_RULES or address_tail or initial_tail or literal_alias or evidence_short and identity == member or structured is not None
                                    and structured[1] in structured_kinds
                                    or identity in families.aliases
                                    and final(reference_roots[families.aliases[identity][0]]) == destination)
                    # OCR families: the correct spelling is far more frequent than its
                    # misreadings, and a misreading repeated through one thread is itself
                    # 'frequent'. A spelling-level rule may move the rarer spelling into a
                    # >=3x more frequent one; attested-surname guards above still apply.
                    dominated = (label_counts[destination] >= DOMINANCE * max(1, label_counts[identity])
                                 and dominant_spelling_ok(value[1], names[identity], names[destination])
                                 # a pure look-alike misreading needs only 2x ('labor Al Thani' 46 / 'Jabor' 98)
                                 or label_counts[destination] >= 2 * max(1, label_counts[identity])
                                 and value[1] in SPELLING_RULES and confusable_equal(names[identity], names[destination])
                                 # re-read from the page scans by >=2 engines: the target need only be as frequent
                                 or value[1] == 'ocr_scan_consensus' and label_counts[destination] >= label_counts[identity])
                    if identity in reference_anchors and not family_alias and not dominated and not same_family_person(names[identity], names[destination]):
                        rejected(member, destination, value[1], 'frozen_reference', identity)
                        return  # a frozen reference must not drift through a later fuzzy rule
                    if (reviewed.get(identity, {}).get('verdict') == 'ok_person'
                            and not context_alias
                            and not (structural_alias and variant[1] != 'surname_one_ocr_edit')
                            and not same_family_person(names[identity], names[destination])):
                        rejected(member, destination, value[1], 'reviewed_person', identity)
                        return
                    if (label_counts[identity] >= MIN_REFERENCE_NAME and label_counts[destination] >= MIN_REFERENCE_NAME
                            and len(families.words[identity]) >= 2 and len(families.words[destination]) >= 2
                            and not family_alias and not dominated and not same_family_person(names[identity], names[destination])):
                        rejected(member, destination, value[1], 'different_frequent_names', identity)
                        return  # also protect full names with short surnames (Ng / Ang)
                    if identity in families.references and destination in families.references:
                        if not family_alias and not dominated and families.references[identity] != families.references[destination]:
                            rejected(member, destination, value[1], 'different_reference_names', identity)
                            return  # two different frequent full names
                if value[1].startswith('header_name_') and member in names:
                    variant = families.header_variants.match(member)
                    if value[1] == 'header_name_ocr_family:truncated_context':
                        own_words = families.words[member]
                        own_firsts = own_words if len(own_words) == 2 else own_words[:1]
                        other_first = families.words[destination][0]
                        if not any(family_ocr_key(NICKNAMES.get(first, first))
                                   == family_ocr_key(NICKNAMES.get(other_first, other_first))
                                   for first in own_firsts):
                            rejected(member, destination, value[1], 'composite_given_name')
                            return  # a reviewed composite label is not a new given-name alias
                    if destination in families.references:
                        given, surname = families.references[destination]
                        own_words = families.words[member]
                        prefix = family_ocr_key(''.join(w for w in own_words[1:] if len(w) > 1))
                        if (own_words and family_ocr_key(own_words[0]) == given
                                and len(prefix) >= 3 and surname.startswith(prefix) and prefix != surname
                                and not (variant is not None and variant[1] in {'middle_name', 'boundary_junk', 'cut_word'})
                                and not families.shared_context(member, destination)):
                            rejected(member, destination, value[1], 'truncation_without_context')
                            return  # legacy matchers cannot bypass context for a cut-off surname
                    if (value[1] in ('header_name_truncated', 'header_name_one_edit', 'header_name_two_edits')
                            and len(families.words[member]) >= 2 and label_counts[destination] < MIN_REFERENCE_NAME):
                        rejected(member, destination, value[1], 'minimum_reference_evidence')
                        return
                    own, other = families.words[member], families.words[destination]
                    surname = families.references.get(destination, ('', ''))[1]
                    surname_end = next((n for n in range(1, len(own)) if surname
                                        and family_ocr_key(''.join(w for w in own[1:n + 1] if len(w) > 1)).startswith(surname)), len(own) - 1)
                    own_initials = tuple(family_ocr_key(w) for w in own[1:surname_end] if len(w) == 1)
                    other_initials = tuple(family_ocr_key(w) for w in other[1:-1] if len(w) == 1)
                    if own_initials and other_initials and own_initials != other_initials:
                        rejected(member, destination, value[1], 'conflicting_middle_initials')
                        return
                    if (value[1] in ('header_name_truncated', 'header_name_one_edit', 'header_name_two_edits')
                            and len(own) == len(other) == 2 and own[0] == other[0]
                            and 1 < len(own[-1]) < 5 and other[-1].startswith(own[-1])):
                        rejected(member, destination, value[1], 'bare_short_surname')
                        return
                    scores = families.candidates(member)
                    # A frequent misspelling is a reference itself: its own entry
                    # (distance 0) is not a competing target.
                    scores = {i: s for i, s in scores.items() if i != member and final(reference_roots.get(i, i)) != member}
                    if scores and value[1] not in ('header_name_address_tail', 'header_name_address_initial_tail') and not evidence_short:
                        distance = min(s[0] for s in scores.values())
                        best = {final(reference_roots[i]) for i, s in scores.items() if s[0] == distance}
                        if best != {destination}:
                            rejected(member, destination, value[1], 'family_nearest_tie_or_other_target')
                            return  # a later legacy rule must not evade a family tie
            super().__setitem__(member, value)
            target, seen = member, set()
            while target in self and target not in seen:
                seen.add(target)
                target = self[target][0]
            cycle = target in seen
            relevant = {pair for i in moved for pair in blocked_by_identity.get(i, ())}
            if cycle or any(len({final(i) for i in pair}) == 1 for pair in relevant):
                rejected(member, destination, value[1], 'cycle' if cycle else 'transitive_separation')
                if previous is missing:
                    super().__delitem__(member)
                else:
                    super().__setitem__(member, previous)
            else:
                if previous is not missing:
                    self.incoming[previous[0]].discard(member)
                self.incoming[value[0]].add(member)

    mapping = GuardedMerges()
    for member, (canonical, item) in sorted(curated.items(), key=lambda row: str(row[0])):
        if isinstance(member, str):
            mapping[member] = (f'kg:{canonical}' if isinstance(canonical, int) else canonical,
                               'reviewed_identity_merge', item['confidence'])
    counts = Counter()
    for column in ('source_id', 'target_id'):
        counts.update(dict(db.execute(f"SELECT {column}, count(*) FROM evidence WHERE {column} LIKE 'email_%' AND relationship_type!='header_name_association' GROUP BY {column}")))
    for identity, (target, kind) in address_family_merges(email_values, counts).items():
        if identity not in mapping:
            confidence = 'needs_review' if identity in flagged else 'probable'
            same_gmail = (gmail_mailbox(email_values[identity]) is not None
                          and gmail_mailbox(email_values[identity]) == gmail_mailbox(email_values[target]))
            rule = ('same_gmail_mailbox' if same_gmail else 'ocr_variant_of_more_frequent_address'
                    if kind == 'same_repaired_mailbox' else 'ocr_address_family:' + kind)
            mapping[identity] = (target, rule, 'confirmed' if same_gmail else confidence)

    for identity, target in domain_typo_merges(email_values, counts).items():
        if frozenset((identity, target)) not in blocked and identity not in mapping:
            mapping[identity] = (target, 'ocr_domain_typo', 'needs_review' if identity in flagged else 'probable')

    # Reviewed audit merges first, then the automatic name-variant rules.
    for identity, item in reviewed.items():
        if 'merge_into' in item and identity not in mapping:
            mapping[identity] = (f"kg:{item['merge_into']}", 'reviewed_audit_merge', 'reviewed')
    # Freeze the reference set and rank every candidate before any automatic name
    # rule runs. Collapse reviewed aliases when deciding uniqueness, never fuzzy
    # aliases created by this pass. Reference spellings cannot drift through chains.
    reference_roots = {i: final(i) for i in families.references}
    # Spellings re-read from the page scans by >=2 independent OCR engines.
    from ocr_consensus import Consensus
    for member, target in Consensus().pairs({**(entity_names or {}), **(participant_labels or {}), **email_values}):
        if member not in mapping and frozenset((member, target)) not in blocked:
            mapping[member] = (target, 'ocr_scan_consensus', 'confirmed')
    equivalent = defaultdict(set)
    by_full_name = defaultdict(set)
    for i, key in families.references.items():
        if i in families.aliases:
            continue
        initials = tuple(w for w in families.words[i][1:-1] if len(w) == 1)
        equivalent[key, initials].add(i)
        by_full_name[key].add(i)
    for ids in equivalent.values():
        roots = {reference_roots[i] for i in ids}
        anchors = {i for i in roots if i.startswith(('kg:', 'company_')) or i not in ids}
        if len(anchors) <= 1:
            canonical = next(iter(anchors)) if anchors else max(ids, key=lambda i: (label_counts[i], i))
            for i in ids:
                reference_roots[i] = canonical
    for ids in by_full_name.values():
        people = {reference_roots[i] for i in ids if reference_roots[i].startswith('kg:')}
        if len(people) == 1:
            person = next(iter(people))
            initials = tuple(w for w in families.words.get(person, ())[1:-1] if len(w) == 1)
            for i in ids:
                own = tuple(w for w in families.words[i][1:-1] if len(w) == 1)
                if not own or not initials or own == initials:
                    reference_roots[i] = person
    for identity, (target, _) in families.aliases.items():
        reference_roots[identity] = reference_roots[target]
    reference_anchors = {reference_roots[i] for i in families.references if i not in families.aliases} & names.keys()
    def merge_name_families():
        for identity in sorted(participant_labels or {}):
            if final(identity) != identity:
                continue
            scores = families.candidates(identity)
            if not scores:
                continue
            distance = min(s[0] for s in scores.values())
            best = {final(reference_roots[i]) for i, s in scores.items() if s[0] == distance}
            if (len(best) == 1 and (target := next(iter(best))) != identity
                    and target in names and not target.startswith(('company_', 'email_'))):
                kind = min(s[1] for i, s in scores.items() if s[0] == distance and final(reference_roots[i]) == target)
                mapping[identity] = (target, 'header_name_ocr_family:' + kind, 'probable')
    merge_name_families()
    # Header names that are variants of one graph person.
    for identity, label in (participant_labels or {}).items():
        if couple_words(label.split()):
            continue  # "Bbeln and Kzega Gordon" is the couple, not Brice (same_couple_spelling below)
        entity_id, rule = matcher.match(label) if matcher else (None, None)
        if entity_id is not None and identity not in mapping and frozenset((identity, f'kg:{entity_id}')) not in blocked:
            mapping[identity] = (f'kg:{entity_id}', 'header_name_variant:' + rule, 'probable')

    # Names equal up to OCR confusions, punctuation, spacing or word order
    # ("Vincenzo lozzo" / "Vincenzo Iozzo", "Slayton Bobby" / "Bobby Slayton").
    parent = {}
    def root(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    by_key = defaultdict(set)
    for identity, label in names.items():
        couple = couple_words(label.split())  # couples only match couples ("Lyn and Jlip" is not Lyn)
        letters = re.findall(r'[^\W\d_]+', label)
        if len(letters) > 1 and len(''.join(letters)) <= 4 and min(map(len, letters)) == 1:
            continue  # "j es": a split fragment (here of "Jeffrey E <jeevacation@...>"), too short to say whose
        for key in name_skeletons(label):
            by_key[('couple', key) if couple else key].add(final(identity))
    # A key two graph people already share (glued headers "Jeffrey Epstein Lesley
    # Groff" / "Lesley Groff Jeffrey Epstein") would chain both people's spelling
    # clusters together and block them all, so it links nothing.
    # Header names reviewed into a company-layer node (build_companies) stand for it like a graph person.
    anchored = lambda i: i.startswith(('kg:', 'company_'))
    by_key = {key: ids for key, ids in by_key.items() if sum(map(anchored, ids)) < 2}
    for ids in by_key.values():
        first, *rest = sorted(ids)
        for other in rest:
            parent[root(other)] = root(first)
    clusters = defaultdict(set)
    for ids in by_key.values():
        for identity in ids:
            clusters[root(identity)].add(identity)
    for ids in clusters.values():
        people = [i for i in ids if anchored(i)]
        if len(ids) < 2 or len(people) > 1:
            continue  # two graph people with one spelling: leave for review
        canonical = people[0] if people else max(ids, key=lambda i: (label_counts[i], i))
        for identity in sorted(ids - {canonical}):
            if identity not in mapping and frozenset((identity, canonical)) not in blocked:
                mapping[identity] = (canonical, 'header_name_ocr_skeleton', 'probable')
    # Fuzzy spellings of one more frequent name, all against names not yet merged
    # (no chains), and only for labels that read as names (no "and", "to"...):
    #   truncated     cut off at either end, >= 2 words, covering >= 70% of the
    #                 target or only the last word cut; rare (<= 20 records) or the
    #                 target 5x as frequent ("Amex Centurion Trave", "Ctacor Rso")
    #   one edit      same word count ("Jaboy Y" / "Jbali Y")
    #   two edits     rare spellings (<= 5 records), same first word and surname
    #                 initial, surname >= 7 letters ("Ann Rbav uez" / "Ann Rfomocece")
    #   middle name   same first and last word, words in order ("Sultan bin
    #                 Sulayem" / "Sultan Ahmed bin Sulayem")
    #   short first   "Steve Tisch" / "Steven Tisch"
    # Each needs exactly one candidate that is itself unmerged at that point.
    weight = lambda i: label_counts[i] + (10 ** 6 if i.startswith('kg:') else 0)
    # The looser rules (first initial, address spelling a name) only land on graph
    # people or header names the node audit judged to be a person.
    def reviewed_person(identity):
        return identity.startswith('kg:') or reviewed.get(identity, {}).get('verdict') in ('ok_person', 'variant_of_known', 'fix_label')
    def name_like(label):
        return '?' not in label and not any(w in FUNCTION_WORDS for w in re.findall(r'[a-z]+', label.casefold()))
    words_of = {}  # per unmerged identity, its most-split spelling ("asil zfo" over "afrizp")
    for identity, label in names.items():
        words, root = name_skeleton_words(label), final(identity)
        if words and root in names and name_like(label) and len(words) > len(words_of.get(root, ())):
            words_of[root] = words
    by_skeleton = defaultdict(set)
    for identity, words in words_of.items():
        by_skeleton[''.join(words)].add(identity)
    # "Lesley Groff cza", "Darren Indyke AOL": a name followed by one or two OCR
    # fragments of the address bracket after it on the header line. Judged on the
    # spellings as written (labels are case-folded): up to two trailing words of
    # <= 3 letters go when they cannot be a surname (trailing_fragment); the rest
    # (>= 2 words) must be exactly one other name.
    by_words = defaultdict(set)  # every spelling, not just each cluster's most-split one
    for identity, label in names.items():
        if name_like(label) and final(identity) in names:
            by_words[tuple(name_skeleton_words(label))].add(final(identity))
    for identity in sorted(names):
        if not identity.startswith('participant_') or final(identity) != identity:
            continue
        targets = set()
        for spelling in (spellings or {}).get(identity, ()):
            stripped = strip_trailing_fragments(spelling)  # the fragment may be a function word ('Ian Osborne a')
            core = name_skeleton_words(stripped)
            if stripped != spelling and name_like(stripped) and len(core) >= 2 and len(core[-1]) >= 3:
                targets |= by_words.get(tuple(core), set())
        targets.discard(identity)
        if len(targets) == 1 and frozenset((identity, target := next(iter(targets)))) not in blocked:
            mapping[identity] = (target, 'header_name_trailing_fragment', 'probable')
    # "Sigrid S. McCawley - Boies", "Ykeh Nma I Cellist", "Eva Dubin czli": a full name
    # followed by a firm, a title or OCR noise (strip_name_tail) merges into that name
    # when exactly one graph person or person-like header name has it (initials ignored).
    def core_words(label):
        return tuple(w for w in name_skeleton_words(label) if len(w) > 1)
    def initials_name_like(label):  # 'Paul A. Smith': an initial is no function word
        return name_like(' '.join(w for w in label.split() if len(w.rstrip('.')) > 1))
    by_core = defaultdict(set)
    for identity, label in names.items():
        words = core_words(label)
        if len(words) >= 2 and initials_name_like(label) and final(identity) in names and (
                identity.startswith('kg:') or label_quality(label) == 'ok' and not is_phrase(label)):
            by_core[words].add(final(identity))
    # A lowercase last word is only noise when it is no surname written capitalized
    # elsewhere ('Mervin Dzel crik', 'Jvengu Bdorganl abascal') and it either follows
    # several names ('alla', 'czli', 'calla') or is a long run ('goindonflimiljefins').
    # 'Richard Saul urman' (Wurman) and 'Lisa Cdorf trusts' stay.
    cuts = {identity: [cut for spelling in sorted((spellings or {}).get(identity, ())) if (cut := strip_name_tail(spelling))]
            for identity in names if identity.startswith('participant_')}
    surnames = {w.casefold() for label in [*(person_names or {}).values(), *(s for v in (spellings or {}).values() for s in v)]
                for w in label.split()[1:] if w[:1].isupper() and w[1:].islower()}
    tail_names = defaultdict(set)
    for items in cuts.values():
        for core, kind, tail in items:
            if kind == 'lowercase_noise':
                tail_names[tail].add(core_words(core))
    def noise(tail):
        return tail not in surnames and (len(tail_names[tail]) >= 2 or len(tail) >= 10)
    # Header-name targets (not graph people) must start with a given name two graph or
    # reviewed people have ('Sha Pganz'), or be a capitalized name before a separator
    # ('Ykeh Nma I Cellist'); not 'sunday night - perfect' or 'United States Attorney'.
    # A tail with a given name is another person: "Ocean's Bridge - James".
    def given_key(label):
        words = name_tokens(label)
        folded = name_skeleton_words(NICKNAMES.get(words[0], words[0])) if words else []
        return folded[0] if folded and len(folded[0]) >= 3 else None
    given = Counter(given_key(label) for identity, label in names.items() if len(name_tokens(label)) >= 2
                    and (identity.startswith('kg:') or reviewed.get(identity, {}).get('verdict') in GOOD_PERSON_VERDICTS))
    given = {w for w, n in given.items() if w and n >= 2}
    def given_name(label):
        return given_key(label) in given
    def person_name(label):
        return any(t.startswith('kg:') or given_name(names[t]) for t in by_core.get(core_words(label), ()))
    def capitalized(label):
        return all(w[:1].isupper() for w in label.split() if w[:1].isalpha() and w.casefold() not in NAME_PARTICLES)
    for identity in sorted(cuts):
        if final(identity) != identity:
            continue
        targets, kinds = set(), set()
        for core, kind, tail in cuts[identity]:
            if not initials_name_like(core) or person_name(tail) or (kind == 'lowercase_noise' and not noise(tail)) \
                    or any(w in given for w in name_skeleton_words(tail)):
                continue  # 'Lesley Groff - Jeffrey Epstein' is two people; 'I Apollo Management' is a firm
            words = core_words(core)
            if len(words) >= 2 and len(words[-1]) >= 3:
                targets |= {t for t in by_core.get(words, set()) if t.startswith('kg:') or given_name(names[t])
                            or kind == 'separator' and capitalized(core)}
                kinds.add(kind)
        targets = {t for t in targets if not t.startswith('email_')} - {identity}  # 'Customer Care - mytheresa.com'
        # Spellings of the clean name may still be apart ("Sigrid McCawley", "Sigrid S.
        # McCawley"): the graph person, or a cluster with 5x the records of the rest, takes it.
        ranked = sorted(targets, key=lambda t: (-weight(t), t))
        if len(ranked) > 1 and weight(ranked[0]) >= 5 * max(weight(ranked[1]), 1):
            targets = {ranked[0]}
        if len(targets) == 1 and frozenset((identity, target := next(iter(targets)))) not in blocked:
            mapping[identity] = (target, 'header_name_tail:' + '+'.join(sorted(kinds)), 'probable')
    # "Hello Lynne", "Thx Liam", "EHAGANDI-Jtis Ckeloc", "Cc Rnu Btahad": a greeting or mail
    # tag before a name merges into the one graph name that is the rest. One word must
    # equal a header name exactly; several must be a graph person, a known given name
    # or capitalized throughout.
    by_label = defaultdict(set)
    for identity, label in names.items():
        if final(identity) in names and not final(identity).startswith('email_') and label_quality(label) == 'ok' and not is_phrase(label):
            by_label[' '.join(label.casefold().split())].add(final(identity))
    for identity in sorted(cuts):
        if final(identity) != identity:
            continue
        targets = set()
        for spelling in sorted((spellings or {}).get(identity, ())):
            core = strip_name_head(spelling)
            if not core:
                continue
            words = core_words(core)
            if len(core.split()) == 1:
                targets |= by_label.get(core.casefold(), set())
            elif len(words) >= 2 and initials_name_like(core):
                targets |= {t for t in by_core.get(words, set()) if t.startswith('kg:') or given_name(names[t]) or capitalized(core)}
        targets = {t for t in targets if not t.startswith('email_')} - {identity}
        if len(targets) == 1 and frozenset((identity, target := next(iter(targets)))) not in blocked:
            mapping[identity] = (target, 'header_name_head_noise', 'probable')
    # "HBRK Associates" / HBRK Associates, Inc.: a header name that is a graph company
    # without its legal form, or with the same form, when exactly one graph company has
    # that name. Two forms (Southern Financial LLC / Inc.) stay apart, as do form-less
    # labels that may be the owner ("Jeff Koons" / Jeff Koons LLC).
    companies = defaultdict(list)
    for identity, label in (entity_names or {}).items():
        if identity not in (person_names or {}):
            base, form = legal_form(label)
            if base and form:
                companies[base].append((identity, form))
    for identity, label in sorted((participant_labels or {}).items()):
        if final(identity) != identity:
            continue
        base, form = legal_form(label)
        candidates = companies.get(base, ())
        if not form and not set(base.split()) & COMPANY_WORDS:
            continue
        matches = [i for i, f in candidates if not form or f == form]
        if len(candidates) == 1 and matches and frozenset((identity, matches[0])) not in blocked:
            mapping[identity] = (matches[0], 'header_name_company_legal_form', 'probable')
    forward = sorted(by_skeleton)
    backward = sorted(t[::-1] for t in by_skeleton)
    def extensions(text, ordered, flip):
        start = bisect.bisect_left(ordered, text)
        out = set()
        while start < len(ordered) and ordered[start].startswith(text):
            if ordered[start] != text:
                out |= by_skeleton[ordered[start][::-1] if flip else ordered[start]]
            start += 1
        return out
    def deletion_keys(text, depth):
        keys = {text}
        for _ in range(depth):
            keys |= {k[:i] + k[i + 1:] for k in keys for i in range(len(k))}
        return keys
    deletions = defaultdict(set)
    frequent_deletions = defaultdict(set)  # two-deletion index, only for names worth merging into
    for text, ids in by_skeleton.items():
        if len(text) >= 6:
            for k in deletion_keys(text, 1):
                deletions[k].add(text)
        if len(text) >= 10 and max(weight(i) for i in ids) >= 10:
            for k in deletion_keys(text, 2):
                frequent_deletions[k].add(text)
    by_ends, by_last = defaultdict(list), defaultdict(list)
    for identity, words in words_of.items():
        if len(words) >= 2 and len(words[0]) >= 2 and len(words[-1]) >= 3:
            by_ends[(words[0], words[-1])].append(identity)
        if len(words) == 2 and len(words[1]) >= 3:
            by_last[words[1]].append(identity)
    def last_word_cut(short, long):  # "ctacor rso" / "ctacor rfomocece"
        return len(short) == len(long) >= 2 and short[:-1] == long[:-1] and len(short[-1]) >= 3 and long[-1].startswith(short[-1])
    def inside(short, long):
        it = iter(long)
        return all(w in it for w in short)
    absorbed = set()  # names other spellings already merged into
    def same_surname(a, b):
        return a == b or a.startswith(b) or b.startswith(a) or edit_distance(a, b, 1) <= 1
    for identity, words in sorted(words_of.items(), key=lambda item: weight(item[0])):
        if not identity.startswith('participant_') or final(identity) != identity:
            continue
        text, mine = ''.join(words), weight(identity)
        heavier = lambda c, factor: c != identity and final(c) == c and weight(c) >= factor * max(mine, 1)
        found = []
        if len(words) >= 2 and len(text) >= 8:
            found.append(('header_name_truncated', {c for c in extensions(text, forward, False) | extensions(text[::-1], backward, True)
                                                    if heavier(c, 3) and (mine <= 20 or weight(c) >= 5 * mine)
                                                    and (len(text) >= 0.7 * len(''.join(words_of[c])) or last_word_cut(words, words_of[c]))}))
        if len(text) >= 6:
            near = {t for k in deletion_keys(text, 1) for t in deletions.get(k, ())}
            # One word one letter apart is often another given name (Andrea / Andrew,
            # Bonnie / Connie): short single words only merge a cut-off ending.
            found.append(('header_name_one_edit', {c for t in near if t != text and edit_distance(t, text, 1) == 1 for c in by_skeleton[t]
                                                   if heavier(c, 5) and len(words_of[c]) == len(words)
                                                   and (len(words) > 1 or min(len(t), len(text)) >= 8 or t.startswith(text) or text.startswith(t))}))
        if len(words) >= 2 and len(words[-1]) >= 7 and len(text) >= 10 and mine <= 5:  # rare misreads only
            near = {t for k in deletion_keys(text, 2) for t in frequent_deletions.get(k, ())}
            found.append(('header_name_two_edits', {c for t in near if t != text and edit_distance(t, text, 2) <= 2 for c in by_skeleton[t]
                                                    if heavier(c, 10) and len(words_of[c]) == len(words) and words_of[c][0] == words[0]
                                                    and words_of[c][-1][0] == words[-1][0]}))
        if len(words) >= 2:  # either spelling may be the frequent one
            found.append(('header_name_middle_dropped', {c for c in by_ends.get((words[0], words[-1]), ())
                                                         if heavier(c, 1) and len(words_of[c]) != len(words)
                                                         and (inside(words, words_of[c]) or inside(words_of[c], words))}))
        if len(words) == 2 and len(words[0]) == 1 and len(words[1]) >= 4 and reviewed_person(identity):  # "T Waitt" -> "Ted Waitt"
            written = names[identity][:1]  # the letter as written: the skeleton folds J/I/l together
            found.append(('header_name_first_initial', {c for c in by_last.get(words[1], ())
                                                        if heavier(c, 1) and len(words_of[c][0]) >= 2 and words_of[c][0][0] == words[0]
                                                        and reviewed_person(c) and names.get(c, ' ')[:1] == written}))
        if len(words) == 2:
            found.append(('header_name_short_first_name', {c for c in by_last.get(words[1], ())
                                                           if heavier(c, 1) and min(len(words_of[c][0]), len(words[0])) >= 4 and words_of[c][0] != words[0]
                                                           and (words_of[c][0].startswith(words[0]) or words[0].startswith(words_of[c][0]))}))
        for rule, candidates in found:
            if len(candidates) == 1:
                target = next(iter(candidates))
                # A name others merged into only moves on to the same surname (no drift).
                if frozenset((identity, target)) not in blocked and (identity not in absorbed or same_surname(words[-1], words_of[target][-1])):
                    mapping[identity] = (target, rule, 'probable')
                    absorbed.add(target)
                break
            if candidates:
                break  # ambiguous: two possible names, leave it
    # Where a fuzzy merge finally lands must still share the surname: a name
    # merged into a glued header ("Michael J. Glidden ... Michael Ovitz") that
    # itself moved on would otherwise follow it to someone else.
    fuzzy = ('header_name_truncated', 'header_name_one_edit', 'header_name_two_edits',
             'header_name_middle_dropped', 'header_name_short_first_name', 'header_name_first_initial')
    for identity in [i for i, (_, rule, _) in mapping.items() if rule in fuzzy]:
        own, landed = words_of.get(identity), name_skeleton_words(names.get(final(identity), ''))
        a, b = (own or [''])[-1], (landed or [''])[-1]
        if own and landed and not (same_surname(a, b) or edit_distance(a, b, 2) <= 2 or a.endswith(b) or b.endswith(a)):
            del mapping[identity]
    # "Nina K" -> "Nina Keita" when exactly one full name fits the initial, or one
    # outweighs the rest 20 to 1 ("Ann R" -> "Ann Rnuruvuze" over leftover
    # misreads such as "Ann Rgann", "Ann Rwadormot").
    full = defaultdict(set)
    for identity, label in names.items():
        words = name_skeleton_words(label)
        if len(words) == 2 and len(words[1]) >= 3 and (identity.startswith('kg:') or label_counts[identity] >= 3):
            full[(words[0], words[1][0])].add(final(identity))
    group_weight = Counter()
    for identity in names:
        group_weight[final(identity)] += label_counts[identity]  # records, not the graph-person bonus
    def surname_initial(identity):
        words = name_skeleton_words(names.get(identity, ''))
        return words[-1][0] if len(words) >= 2 else None
    for identity, label in (participant_labels or {}).items():
        words = name_skeleton_words(label)
        if len(words) == 2 and len(words[1]) == 1 and len(words[0]) >= 3 and final(identity) == identity:
            top, *rest = sorted(full.get((words[0], words[1]), set()), key=lambda c: -group_weight[c]) or [None]
            if top is None or frozenset((identity, top)) in blocked:
                continue
            if not rest:
                mapping[identity] = (top, 'header_name_initial_unique', 'probable')
            # The winner's own name must fit too, not just a misread merged into it.
            elif group_weight[top] >= 20 * sum(group_weight[c] for c in rest) and surname_initial(top) == words[1]:
                mapping[identity] = (top, 'header_name_initial_dominant', 'probable')

    def weak(identity):
        # "michael", "tom", "andrew l.": would gather every namesake's address, and
        # are too vague to count against a full name either.
        words = (participant_labels or {}).get(identity, '').split()
        return identity.startswith('participant_') and (len(words) < 2 or all(len(w.strip('.')) <= 1 for w in words[1:]))

    # Vague names cannot win an address, but when the ones that cannot be the
    # winner outnumber it, the address is someone else's: 98 "Nadia" and 24
    # "N E M" records against 4 "Jeffrey Epstein" ones on nadja2102@...
    # "Lesley", "Groff" and "LG" fit Lesley Groff.
    def fits(vague_label, name):
        words, full = name_skeleton_words(vague_label), name_skeleton_words(name)
        if not words or not full:
            return True
        letters = ''.join(words)
        return words[0] == full[0] or full[-1] in words or \
            (len(words) == 1 and 2 <= len(letters) <= 4 and letters[0] == full[0][0] and letters[-1] == full[-1][0])
    associations, vague = defaultdict(Counter), defaultdict(Counter)
    for person, email, n in db.execute("""SELECT source_id, target_id, count(*) FROM evidence
            WHERE relationship_type IN ('header_label_association','header_name_association','upstream_resolved_header_identity') GROUP BY 1, 2"""):
        (vague if weak(final(person)) else associations)[final(email)][final(person)] += n
    for email, people in associations.items():
        if not email.startswith('email_'):
            continue
        (person, n), total = people.most_common(1)[0], sum(people.values())
        # OCR-damaged spellings of the winning name ("lesley gron") agree with it.
        top = name_key((entity_names or {}).get(person) or (participant_labels or {}).get(person, ''))
        n += sum(m for other, m in people.items() if other != person and len(top) >= 8
                 and edit_distance(name_key((participant_labels or {}).get(other, '')), top) <= 2)
        named, against = n, sum(m for other, m in vague[email].items() if not fits(names.get(other, ''), top))
        n += sum(vague[email].values()) - against
        if against > n:
            continue
        total += sum(vague[email].values()) - against
        # One stray record (a quoted reply header) does not veto four.
        if named >= 3 and (n >= 0.9 * total or n >= 4 and total - n <= 1) and not person.startswith('email_') and frozenset((email, person)) not in blocked:
            mapping[email] = (person, 'header_names_point_to_one_identity', 'probable')
    # One Apple account answers at me.com, mac.com, mail.mac.com and icloud.com:
    # an alias of an address that resolved to a person is the same mailbox
    # (scenul427@mail.mac.com beside scenul427@me.com). Exact local part only.
    def mailbox(value):
        local, domain = ocr_repair(value) if '@' in value else ('', '')
        return local if domain in APPLE_DOMAINS and local else None
    owners = defaultdict(set)
    for identity, value in email_values.items():
        if mailbox(value) and not final(identity).startswith('email_'):
            owners[mailbox(value)].add(final(identity))
    for identity, value in email_values.items():
        found = owners.get(mailbox(value), set()) if mailbox(value) else set()
        if final(identity) == identity and len(found) == 1 and frozenset((identity, next(iter(found)))) not in blocked:
            mapping[identity] = (next(iter(found)), 'same_account_alias_domain', 'probable')
    # An address whose local part spells one person's name (rweingarten@,
    # reid.weingarten@, weingartenr@) belongs to them when header names on it say
    # so, or when another of their addresses uses the same domain. Surname >= 5 letters; one candidate, or one outweighing the
    # rest 20 to 1 (Reid over a rare Ross Weingarten).
    owned_domains = defaultdict(set)  # person -> domains of addresses already resolved to them
    for identity, value in email_values.items():
        if '@' in value and not final(identity).startswith('email_'):
            owned_domains[final(identity)].add(ocr_repair(value)[1])
    spelled = defaultdict(set)
    for identity, label in names.items():
        words = name_skeleton_words(label)
        target = final(identity)
        if len(words) == 2 and len(words[0]) >= 2 and len(words[1]) >= 5 and not target.startswith('email_') \
                and (identity.startswith('kg:') or label_counts[identity] >= 3 and reviewed_person(identity)):
            first, last = words
            for local in (first[0] + last, first + last, last + first[0], last + first):
                spelled[local].add(target)
    for identity, value in email_values.items():
        if final(identity) != identity or '@' not in value:
            continue
        local = re.sub(r'[^a-z0-9]', '', ocr_repair(value)[0])
        top, *rest = sorted(spelled.get(local, ()), key=lambda c: -group_weight[c]) or [None]
        if top is None or (rest and group_weight[top] < 20 * sum(group_weight[c] for c in rest)):
            continue
        said = associations.get(identity)
        if said and final(said.most_common(1)[0][0]) != top:
            continue  # header names on the address point at someone else
        # A namesake elsewhere (Lsoloc.Gnece@example.test) is not the person: the
        # header names must agree, or the domain already serve their own address.
        domain = ocr_repair(value)[1]
        if not said and domain not in owned_domains.get(top, set()):
            continue
        if frozenset((identity, top)) not in blocked:
            mapping[identity] = (top, 'address_spells_one_name', 'probable')
    # A display name that is really an address's local part ("Je vacation" for
    # jeevacation@...) belongs to whoever that address resolved to (exact or one
    # edit, local part >= 8 letters, exactly one owner).
    owner_of = defaultdict(set)
    for identity, value in email_values.items():
        local, owner = ''.join(name_skeleton_words(ocr_repair(value)[0])), final(identity)
        if len(local) >= 8 and not owner.startswith('email_'):
            owner_of[local].add(owner)
    near_local = defaultdict(set)
    for local in owner_of:
        for k in deletion_keys(local, 1):
            near_local[k].add(local)
    name_addresses = defaultdict(set)
    address_domains = defaultdict(set)
    for identity, value in email_values.items():
        if '@' in value:
            local, domain = ocr_repair(value)
            address_domains[''.join(name_skeleton_words(local)), final(identity)].add(
                ''.join(name_skeleton_words(domain)))
    for local, owners in owner_of.items():
        for owner in owners:
            if owner in families.references and owner not in families.aliases:
                name_addresses[families.references[owner][0]].add((local, owner))
    for identity in sorted(participant_labels or {}):
        words = families.words[identity]
        if final(identity) != identity or len(words) < 2 or len(words[0]) < 2:
            continue
        targets = set()
        for local, owner in name_addresses.get(family_ocr_key(words[0]), ()):
            tails = [tail for tail, _ in families.prefix_tails(words, owner)]
            if (len(words) >= 3 and len(words[1]) == 1
                    and family_ocr_key(words[1]) == family_ocr_key(families.words[owner][-1][0])):
                tails.append(' '.join(words[2:]))
            if (len(words[1]) > 1
                    and family_ocr_key(words[1][0]) == family_ocr_key(families.words[owner][-1][0])):
                tails.append(' '.join((words[1][1:], *words[2:])))
            for tail in tails:
                text = re.sub(r'^(?:imailto|mailto)', '', ''.join(name_skeleton_words(tail)))
                limit = 2 if len(local) >= 10 else 1
                # The owner validates the address local part, even if its OCR
                # fragments happen to look like surnames ("Lee Vacation"). Check
                # the rest separately so a second person still vetoes the merge.
                def domain_remainder(remainder):
                    return any(edit_distance(domain, remainder[start:end], 1) <= 1
                               for domain in address_domains[local, owner]
                               for start in (0, 1) for end in (len(remainder), len(remainder) - 1)
                               if end > start)
                if any(edit_distance(local, text[:n], limit) <= limit
                       and (not text[n:] or families.junk_tail(text[n:], owner)
                            or domain_remainder(text[n:]))
                       for n in range(max(8, len(local) - limit), len(local) + limit + 1)):
                    targets.add(owner)
        if len(targets) == 1:
            mapping[identity] = (next(iter(targets)), 'header_name_address_tail', 'probable')
    for identity, label in (participant_labels or {}).items():
        text = ''.join(name_skeleton_words(label))
        if final(identity) != identity or len(text) < 8:
            continue
        locals_ = {t for k in deletion_keys(text, 1) for t in near_local.get(k, ()) if edit_distance(t, text, 1) <= 1}
        owners = {o for t in locals_ for o in owner_of[t]} - {identity}
        if len(owners) == 1 and frozenset((identity, next(iter(owners)))) not in blocked:
            mapping[identity] = (next(iter(owners)), 'header_name_is_address', 'probable')
    # A header name that is an address whose "@" OCR misread as one character
    # ("jroeRexample.com", "jroeegmail.com") belongs to that address's identity.
    by_address = defaultdict(set)
    for identity, value in email_values.items():
        if '@' in value:
            local, domain = ocr_repair(value)
            by_address[local, domain].add(final(identity))
    address_locals, domain_cache = {local for local, _ in by_address}, {}
    for identity, label in (participant_labels or {}).items():
        text = label.replace(' ', '')
        if final(identity) != identity or '@' in text or len(text) < 10:
            continue
        found = set()
        for cut in range(3, len(text) - 5):  # local part >= 3 chars, "x.com" after the misread character
            local, domain = text[:cut].casefold(), text[cut + 1:]
            if local in address_locals and '.' in domain:
                repaired = domain_cache.get(domain) or domain_cache.setdefault(domain, ocr_repair('x@' + domain)[1])
                found |= by_address.get((local, repaired), set())
        found.discard(identity)
        if len(found) == 1 and frozenset((identity, next(iter(found)))) not in blocked:
            mapping[identity] = (next(iter(found)), 'header_name_is_misread_address', 'probable')
    # A header name that is a damaged address: optional owner-name prefix, the local
    # part (<= 2 OCR edits, "eevacation c mail.com", "Jeffrey Epsteinijeevacationegmail.com"),
    # then the "@" and a damaged domain. Local part >= 10 letters, exactly one owner.
    mangled_index = defaultdict(set)
    for local in owner_of:
        if len(local) >= 10:
            for k in deletion_keys(local, 2):
                mangled_index[k].add(local)
    owner_words = {}
    for owner in {o for owners in owner_of.values() for o in owners}:
        text = (names.get(owner) or (person_names or {}).get(owner) or '')
        owner_words[owner] = ''.join(name_skeleton_words(text))
    domain_hint = re.compile(r'(?:mail|ail|nail|gmal|yahoo|com|corn|\.c\b|\.co\b)', re.I)
    for identity, label in (participant_labels or {}).items():
        if final(identity) != identity or not domain_hint.search(label):
            continue
        text = ''.join(name_skeleton_words(label))
        found = set()
        for start in range(0, min(len(text) - 9, 24)):
            for n in range(10, min(len(text) - start, 16) + 1):
                piece = text[start:start + n]
                for local in {t for k in deletion_keys(piece, 2) for t in mangled_index.get(k, ())}:
                    if edit_distance(local, piece, 2) > 2:
                        continue
                    for owner in owner_of[local]:
                        prefix, rest = text[:start], text[start + n:]
                        core = prefix[:-2] if len(prefix) > 2 and not owner_words[owner].endswith(prefix) else prefix
                        prefix_ok = (not prefix or len(prefix) <= 2 or owner_words[owner].find(core) >= 0
                                     and len(core) >= 3 or any(p.startswith(core) for p in owner_words[owner].split()))
                        domains = address_domains[local, owner]
                        rest_ok = len(rest) <= 3 or any(
                            edit_distance(domain[a:b], rest, 1) <= 1 for domain in domains
                            for a in range(0, 3) for b in range(max(a + 1, len(domain) - 3), len(domain) + 1))
                        if prefix_ok and rest_ok and owner != identity:
                            found.add(owner)
        if len(found) == 1 and frozenset((identity, next(iter(found)))) not in blocked:
            mapping[identity] = (next(iter(found)), 'header_name_mangled_address', 'probable')
    # A header name written almost only on one identity's addresses, sharing its
    # first name, is that identity's sign-off ("Jeffrey", "jeffrey E." on
    # Epstein's mailboxes: 99.8% of 7,249 records; "Sarah K": 88 of 94 on Sarah
    # Ktisba's). >= 20 records and >= 95%, or >= 50 and >= 90%; an
    # assistant's own name on the boss's mailbox fails the first-name check.
    on_addresses = defaultdict(Counter)
    for label_id, address, n in db.execute("""SELECT source_id, target_id, count(*) FROM evidence
            WHERE relationship_type IN ('header_label_association','header_name_association')
            AND source_id LIKE 'participant_%' GROUP BY 1, 2"""):
        on_addresses[final(label_id)][final(address)] += n
    for identity, owners in on_addresses.items():
        if not identity.startswith('participant_') or final(identity) != identity:
            continue
        (owner, n), total = owners.most_common(1)[0], sum(owners.values())
        own, theirs = name_skeleton_words(names.get(identity, '')), name_skeleton_words(names.get(owner, ''))
        # The looser share also needs the surname (or its initial) to fit: "Sarah K".
        surname_fits = len(own) >= 2 and len(theirs) >= 2 and theirs[-1].startswith(own[-1])
        if n >= 20 and (n >= 0.95 * total or n >= 50 and n >= 0.9 * total and surname_fits) and not owner.startswith('email_') and final(owner) != identity \
                and own and theirs and len(own[0]) >= 3 and own[0] == theirs[0] \
                and frozenset((identity, owner)) not in blocked:
            mapping[identity] = (owner, 'header_name_on_one_identity_addresses', 'probable')
    # A one-word or initials header name ("svetlana", "Gmax", "Rich") written almost only
    # on one identity's addresses is that identity's sign-off: >= 90% of >= 3 records.
    # A person or full-name owner must also fit the name ("Rich" / Richard Kahn) or the
    # address must contain it ("gmax" in gmax1@...); an unresolved address only needs
    # >= 5 records (or to contain the name), and the address is the fuller label.
    raw_addresses = defaultdict(set)
    for label_id, address in db.execute("""SELECT DISTINCT source_id, target_id FROM evidence
            WHERE relationship_type IN ('header_label_association','header_name_association')
            AND source_id LIKE 'participant_%' AND target_id LIKE 'email_%'"""):
        raw_addresses[final(label_id)].add(address)
    for identity, owners in on_addresses.items():
        if not identity.startswith('participant_') or final(identity) != identity or not weak(identity):
            continue
        (owner, n), total = owners.most_common(1)[0], sum(owners.values())
        own_words = name_skeleton_words(names.get(identity, ''))
        own = ''.join(own_words)
        if len(own) < 3 or final(owner) == identity or frozenset((identity, owner)) in blocked:
            continue
        # Unresolved OCR variants of the winning address (rgcaffrey / rqcaffrey / rscaffrev,
        # truncated oniek@ for yoniek@) count for it.
        local = lambda a: (''.join(name_skeleton_words(ocr_repair(email_values[a])[0]))
                           if '@' in email_values.get(a, '') else '')
        if owner.startswith('email_') and len(local(owner)) >= 5:
            top = local(owner)
            n += sum(m for other, m in owners.items() if other != owner and other.startswith('email_') and local(other)
                     and (local(other) in top or edit_distance(local(other), top) <= 2))
        in_address = any(own in local(a) for a in raw_addresses[identity] if final(a) == owner and a in email_values)
        if in_address and len(own) >= 4:
            # Unmerged garbled copies holding the name (gveng@example.test beside gmax1@) agree too.
            n += sum(m for other, m in owners.items() if other != owner and other.startswith('email_') and own in local(other))
        if n < 3 or n < (0.8 if in_address else 0.9) * total:
            continue
        theirs = name_skeleton_words(names.get(owner, ''))
        # "Brad R" is not Bpah Odig: a trailing initial must start the surname.
        initial_ok = len(own_words) < 2 or len(theirs) < 2 or theirs[-1].startswith(own_words[-1])
        fits_name = len(theirs) >= 2 and initial_ok and (theirs[0].startswith(own) or fits(names.get(identity, ''), names.get(owner, '')))
        if in_address or (fits_name if not owner.startswith('email_') else n >= 5):
            mapping[identity] = (owner, 'short_header_name_on_one_identity', 'probable')
    # A contact-backed surname initial can also explain the same display name
    # with a bracket/mailto fragment. Freeze only signatures actually accepted
    # above; a merely unique or dominant name expansion supplies no evidence.
    for identity in on_addresses:
        if (address_short_name(identity, final(identity)) and identity in mapping
                and mapping[identity][1] in ('header_name_on_one_identity_addresses', 'short_header_name_on_one_identity')
                and len(families.words[identity][1]) == 1):
            words = families.words[identity]
            validated_initials[family_ocr_key(words[0]), words[1]].add(final(identity))
    for identity in sorted(participant_labels or {}):
        words = families.words[identity]
        if final(identity) == identity and len(words) >= 3 and len(words[1]) == 1:
            owners = validated_initials[family_ocr_key(words[0]), words[1]]
            if len(owners) == 1 and address_initial_tail(identity, next(iter(owners))):
                mapping[identity] = (next(iter(owners)), 'header_name_address_initial_tail', 'probable')
    # Reconsider frozen ties after address/review evidence has established that
    # their reference nodes are aliases. Scores and reference spellings stay fixed.
    merge_name_families()
    # Spellings of one couple ("Lynn and Jlip", "Lyn & Jlip fnuliririn", "Lynn and Jlip
    # Fontanilla") are one couple node: same two given names up to doubled letters, at
    # most one surname among them; the most frequent spelling with the surname wins.
    collapse = lambda w: re.sub(r'(.)\1+', r'\1', w)
    couples = defaultdict(dict)
    for identity, label in (participant_labels or {}).items():
        words = label.split()
        if couple_words(words):
            root = final(identity)
            key = tuple(sorted((collapse(words[0]), collapse(words[2]))))
            surname = words[3] if len(words) == 4 and collapse(words[3]) not in key else None
            couples[key][root] = max(couples[key].get(root, (None, 0)), (surname, label_counts[identity]), key=lambda x: (x[0] is not None, x[1]))
    for key, roots in couples.items():
        surnames = {collapse(s) for s, _ in roots.values() if s}
        if len(roots) < 2 or len(surnames) > 1 or not all(r.startswith('participant_') for r in roots):
            continue
        canonical = max(roots, key=lambda r: (roots[r][0] is not None, label_counts[r], r))
        for root in roots:
            if root != canonical and frozenset((root, canonical)) not in blocked:
                mapping[root] = (canonical, 'same_couple_spelling', 'probable')
    # A company's automated senders (noreply@, messages-noreply@bounce., info@) are the
    # company: one identity per company domain. It joins the organization header name
    # the node review confirmed on those addresses ("Wayfair" on emobac@example.test),
    # else the most frequent address. Addresses already resolved to a person stay theirs.
    companies = defaultdict(list)
    for identity, value in email_values.items():
        company = automated_company(value)
        if company and final(identity) == identity:
            companies[company].append(identity)
    for company, addresses in companies.items():
        named = Counter()
        for address in addresses:
            for person, n in (associations.get(address, Counter()) + vague.get(address, Counter())).items():
                if reviewed.get(person, {}).get('verdict') == 'ok_entity' and final(person) == person:
                    named[person] += n
        canonical = named.most_common(1)[0][0] if named else max(addresses, key=lambda a: (counts[a], a))
        for address in addresses:
            if address != canonical and frozenset((address, canonical)) not in blocked:
                mapping[address] = (canonical, 'automated_company_sender', 'probable')
    # A frozen, independent context pass can resolve partial names and more
    # distant OCR without relaxing spelling-only rules. Existing explicit and
    # imported separations still veto the entire prospective cluster.
    def context_final(i):
        # Snapshot audits lack spellings which have already disappeared from the
        # viewer. Only those absent raw names can borrow the existing DB's map.
        # A real build supplies all parsed names and does not use this fallback.
        if i not in names and i.startswith('participant_') and support_aliases:
            i = support_aliases.get(i, i)
        if i.startswith('email_'):
            return mailbox_owner(i)
        return final(i)
    def mailbox_root(i):
        seen = set()
        while i in mapping and i not in seen and mapping[i][0].startswith('email_'):
            seen.add(i)
            i = mapping[i][0]
        return i
    def mailbox_owner(i):
        original, seen = i, set()
        while i in mapping and i not in seen:
            seen.add(i)
            target, _, confidence = mapping[i]
            if confidence in ('needs_review', 'uncertain'):
                return original
            i = target
        return i
    context = ContextNames(db, names, label_counts, reviewed, context_final, header_name_words,
                           nickname_root, label_quality,
                           FUNCTION_WORDS | NON_NAME_WORDS | ROLE_TOKENS | COMPANY_WORDS | BOILERPLATE_TOKENS,
                           mailbox_root=mailbox_root, mailbox_values=email_values, mailbox_owner=mailbox_owner)
    if context_corpus:
        def bounded_document(doc):
            with corpus_source.connect() as source:
                rows = source.execute('SELECT substr(text,1,200000) FROM pages WHERE doc_id=? ORDER BY page LIMIT 7',
                                      (doc,)).fetchall()
                return '\n'.join(row[0] or '' for row in rows) if len(rows) <= 6 else ''
        if corpus_source.PATH.exists():
            context.attest_spellings(bounded_document)
        pairs = []
        for identity, label in (participant_labels or {}).items():
            own = header_name_words(label)
            if final(identity) != identity or len(own) != 1 or len(own[0]) < 6:
                continue
            hits, _ = context.candidates(identity)
            if len(hits) == 1:
                target, kind = next(iter(hits.items()))
                if kind == 'handle' and not context.mailbox_ok(identity, target):
                    pairs.append((identity, target, label, names[target]))
        context.quoted = quoted_handle_documents(pairs)
    proposals = {i: proposal for i in sorted(participant_labels or {}) if final(i) == i
                 and (proposal := context.propose(i)) is not None}
    # A previously absorbed spelling must independently pass the same evidence
    # test before a new context merge may carry its cluster to a new reference.
    for identity in participant_labels or {}:
        owner = final(identity)
        if owner in proposals and identity != owner:
            if (alias := context.propose(identity)) is not None and alias[0] == proposals[owner][0]:
                context_confirmed[identity] = alias[0]
    for identity, (target, rule) in proposals.items():
        context_confirmed[identity] = target
        mapping[identity] = (target, rule, 'probable')
        if diagnostics is not None and identity in mapping and mapping[identity][1] == rule:
            diagnostics.append({**context.last_details[identity], 'member': identity,
                                'rule': rule, 'reason': 'accepted_context'})
    return {member: (final(member), rule, confidence) for member, (_, rule, confidence) in mapping.items()
            if final(member) != member}


def resolved_header_compounds(names, final):
    """Pseudonymous constituent links; the compound itself remains a node."""
    forbidden = (FUNCTION_WORDS | NON_NAME_WORDS | ROLE_TOKENS | COMPANY_WORDS | BOILERPLATE_TOKENS
                 | {'llc', 'llp', 'inc', 'ltd', 'corp', 'airlines', 'customer', 'service'}) - NAME_PARTICLES
    refs = {i for i, label in names.items() if label_quality(label) == 'ok'
            and 2 <= len(header_name_words(label)) <= 4
            and not any(w in forbidden for w in header_name_words(label))}
    policy = ROOT / 'config/survivors.json'
    if not policy.exists():
        return {}  # without exclusions, add no compound overlays
    protected = {name_key(n) for n in json.loads(policy.read_text())['names']}
    excluded = {i for i, label in names.items() if name_key(label) in protected}
    dictionary, proper, _ = dictionary_words()
    return compound_people(names, final, header_name_words, refs, excluded, forbidden, dictionary - proper)


FIRST_NAME_WORD = re.compile(r'^[^\W\d_]{3,}$')
SURNAME_WORD = re.compile(r"^[^\W\d_][^\W\d_'-]+$")
TEXT_ADDRESS = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
MAX_CONTEXT_PAGES = 6  # page text of longer documents (mail archives) names too many people
# Second words of header labels that are form fields or phrases, not surnames ("Skype Name").
FORM_WORDS = {'name', 'number', 'copies', 'ticket', 'tickets', 'acct', 'account', 'alerts', 'digest', 'store',
              'holdings', 'llc', 'inc', 'ventures', 'immediately', 'all', 'skype'}
BARE_DOMAIN = re.compile(r'[a-z0-9-]{3,}\.(?:com|org|net|edu|gov)')
GOOD_PERSON_VERDICTS = {'ok_person', 'variant_of_known'}
REVIEWED_NAMES = {*GOOD_PERSON_VERDICTS, 'ok_entity', 'fix_label'}


def bare_first_name(label, verdict):
    """A one-word header label that may be a first name ("Melanie", not "Airline")."""
    return (len(label.split()) == 1 and verdict in (None, *GOOD_PERSON_VERDICTS) and label_quality(label) == 'ok'
            and label.casefold() not in FORM_WORDS)


def context_person(label, verdict, attested):
    """Whether a participant label may receive a bare first name from its document: a
    reviewed person, or an unreviewed clean "First Last" (optionally a middle initial)
    that is attested (written with an address, or a frequent whole header name)."""
    if verdict in GOOD_PERSON_VERDICTS:
        return True
    words = re.findall(r"[^\W\d_]+", label.casefold())
    if verdict or not attested or label_quality(label) != 'ok' or not 2 <= len(words) <= 3:
        return False
    if len(words) == 3 and len(words[1]) != 1:
        return False
    return all(len(w) >= 2 and w not in FUNCTION_WORDS | NON_NAME_WORDS | FORM_WORDS for w in (words[0], words[-1]))


@lru_cache(maxsize=None)
def name_word_patterns(word):
    """("<word> <Next>", "<Previous> <word>") patterns for first_name_owners; compiled
    once per word (re's own cache holds 512 and was recompiling ~100k times)."""
    return (re.compile(r'\b' + re.escape(word) + r"\s+([^\W\d_][^\W\d_'-]+)", re.I),
            re.compile(r"([^\W\d_][^\W\d_'-]+)\s+" + re.escape(word) + r'\b', re.I))


def first_name_owners(evidence_rows, labels, final, email_ids, document_text):
    """{(row id, 'source'|'target'): identity} for message records whose endpoint is a bare
    first name or surname ("Melanie", "Dubin") and whose document names exactly one person
    with that name.

    A bare name is shared by everyone called Melanie, so it is attributed record by record
    from the same document: other header endpoints (canonical), "Melanie <Surname>" /
    "<First> Dubin" in the page text where that full name is a known identity, and addresses
    quoted in the page text. Nothing is assigned when the document names two such people
    or none, except that a bare surname goes to the one couple of that name the document
    names ("Fontanilla" beside Lynn, Jojo and "Lynn and Jlip Fnuliririn").

    evidence_rows: (row id, efta, canonical source, canonical target); labels: {canonical
    identity: label} for people and participants; final: member -> canonical; email_ids:
    {address: email identity}; document_text(efta): page text or '' (long documents skipped)."""
    def words(identity):
        return (labels.get(identity) or '').split()
    bare = {i: w[0].casefold() for i in labels if i.startswith(('participant_', 'kg:')) and len(w := words(i)) == 1 and FIRST_NAME_WORD.match(w[0])}
    by_full = defaultdict(set)
    parts, couple_surname = {}, {}
    for identity in labels:
        w = words(identity)
        if identity in bare or len(w) < 2:
            continue
        if couple_words(w):
            if len(w) == 4:
                couple_surname[identity] = w[3].casefold()
        elif FIRST_NAME_WORD.match(w[0]) and SURNAME_WORD.match(w[-1]):
            parts[identity] = (w[0].casefold(), w[-1].casefold())
            by_full[f'{w[0]} {w[-1]}'.casefold()].add(identity)
    documents = defaultdict(list)
    for row in evidence_rows:
        documents[row[1]].append(row)
    owners = {}
    for efta, rows in documents.items():
        wanted = {bare[x] for row in rows for x in row[2:] if x in bare}
        if not wanted:
            continue
        named = {x for row in rows for x in row[2:]}
        text = document_text(efta) if efta else ''
        for word in wanted:
            before, after = name_word_patterns(word)
            for match in before.finditer(text):
                named |= by_full.get(f'{word} {match.group(1)}'.casefold(), set())
            for match in after.finditer(text):
                named |= by_full.get(f'{match.group(1)} {word}'.casefold(), set())
        for address in TEXT_ADDRESS.findall(text):
            if (identity := email_ids.get(address.casefold())) is not None:
                named.add(final(identity))
        # Named identities by first name / surname and by couple surname, once per document.
        by_word, couples = defaultdict(set), defaultdict(set)
        for i in named:
            for w in parts.get(i, ()):
                by_word[w].add(i)
            if i in couple_surname:
                couples[couple_surname[i]].add(i)
        for row in rows:
            for side, x, other in (('source', row[2], row[3]), ('target', row[3], row[2])):
                if x not in bare:
                    continue
                found = by_word.get(bare[x], set()) - {other}
                if len(found) > 1 and all(parts[i][1] == bare[x] for i in found):
                    found = couples.get(bare[x], set()) - {other}
                if len(found) == 1:
                    owners[row[0], side] = next(iter(found))
    return owners


class Builder:
    def __init__(self, key, entities, aliases=None):
        self.key = key
        self.entities = {e['id']: e for e in entities}
        self.names = {}
        for entity in entities:
            if entity['entity_type'] not in ('person', 'organization', 'shell_company'):
                continue
            if entity['entity_type'] == 'person' and len(entity['normalized_name'].split()) < 2:
                continue
            self.names.setdefault(entity['normalized_name'], []).append(entity['id'])
        # Spellings folded in by reviewed merges ("Groff", "DARREN INDYKE") are
        # reviewed identities, so they match header names even as one word.
        for entity in entities:
            for original in entity.get('original_names', []):
                key = name_key(original)
                if key and key not in self.names:
                    self.names[key] = [entity['id']]
        # Registry aliases ("Alex Acosta" -> Alexander Acosta) only where unambiguous.
        for entity_id, labels in (aliases or {}).items():
            for label in labels:
                key = name_key(label)
                if len(key.split()) >= 2 and key not in self.names:
                    self.names[key] = [entity_id]
        self.known_cache = {}
        self.nodes = {}
        self.email_values = {}
        self.participant_labels = {}
        self.participant_spellings = defaultdict(set)
        self.nonparty_ids = set()
        self.party_ids = set()
        self._marking_nonparty = False
        self.dropped_labels = 0
        self.recovered_labels = 0
        self.matcher = PersonMatcher(entities) if entities else None
        self.review = node_review()['participants']
        self.endpoint_cache = {}

    def known_name(self, name):
        # self.names is fixed after __init__; header names recur millions of times.
        if name not in self.known_cache:
            matches = self.names.get(name_key(name or ''), [])
            self.known_cache[name] = matches[0] if len(matches) == 1 else None
        return self.known_cache[name]

    def participant_id(self, label):
        return 'participant_' + hmac.new(self.key, ('header_label\0' + label).encode(), hashlib.sha256).hexdigest()[:24]

    def label_identity(self, name):
        """Identity a header display name points to (graph person or participant),
        without counting it as a dropped or recovered label."""
        if not name:
            return None
        counters = self.dropped_labels, self.recovered_labels
        identity = self.endpoint(name, None)
        self.dropped_labels, self.recovered_labels = counters
        return identity

    def association(self, known, name, email_id):
        """(identity, email_id, kind) for a header name shown with an address, or None.
        Exact graph-person names are header_label_association edges; other names
        (participants, recovered OCR names) are header_name_association evidence,
        used only to resolve identities and never drawn as edges."""
        if known is not None:
            return self.entity_identity(known), email_id, 'header_label_association'
        identity = self.label_identity(name)
        return (identity, email_id, 'header_name_association') if identity else None

    def mark_nonparty(self, name):
        """Retain a swallowed body/signature label without inventing a message edge."""
        self._marking_nonparty = True
        try:
            identity = self.endpoint(name, None)
        finally:
            self._marking_nonparty = False
        if identity and identity.startswith('participant_'):
            self.nonparty_ids.add(identity)
        return identity

    def entity_identity(self, entity_id):
        identity = 'kg:' + str(entity_id)
        entity = self.entities[entity_id]
        self.nodes.setdefault(identity, (identity, entity['entity_type'], entity['name']))
        return identity

    def endpoint(self, name, address):
        """Identity for a header (name, address), or None. Memoized: the same
        headers recur millions of times. The first call registers the node; hits
        replay only the per-occurrence label counters."""
        key = (name, address)
        cached = self.endpoint_cache.get(key)
        if cached is None:
            dropped, recovered = self.dropped_labels, self.recovered_labels
            identity = self._endpoint(name, address)
            self.endpoint_cache[key] = (identity, self.dropped_labels - dropped, self.recovered_labels - recovered)
            if identity and not self._marking_nonparty:
                self.party_ids.add(identity)
            return identity
        identity, dropped, recovered = cached
        self.dropped_labels += dropped
        self.recovered_labels += recovered
        if identity and not self._marking_nonparty:
            self.party_ids.add(identity)
        return identity

    def _endpoint(self, name, address):
        email = valid_email(address)
        if email:
            identity = contact_id(email, 'email', self.key)
            self.nodes.setdefault(identity, (identity, 'email', 'Email_' + identity.split('_', 1)[1]))
            self.email_values.setdefault(identity, email)  # memory only, for OCR clustering
            return identity
        known = self.known_name(name)
        if known is not None:
            return self.entity_identity(known)
        label = name_key(name or '')
        # Unknown names are pseudonymized, not asserted to be real people.
        if not 2 <= len(label) <= 80 or len(label.split()) > 6:
            return None
        if not all(c.isalpha() or c in " .'-" for c in label) or sum(c.isalpha() for c in label) < 2:
            return None
        if domain_fragment(label):
            return None  # "mail.com": the cut-off tail of an address, not a person
        if 'redact' in label or label in ('undisclosed recipients', 'undisclosed-recipients:', 'unknown', 'to', 'from', 'none'):
            return None
        if label_quality(label) == 'drop':
            entity_id, _rule = self.matcher.match(label) if self.matcher else (None, None)
            if entity_id is not None:
                self.recovered_labels += 1
                return self.entity_identity(entity_id)  # garbled but recoverable name
            self.dropped_labels += 1
            return None
        identity = self.participant_id(label)
        digest = identity.split('_', 1)[1]
        if self.review.get(identity, {}).get('verdict') in DROP_VERDICTS:
            self.dropped_labels += 1
            return None
        self.nodes.setdefault(identity, (identity, 'participant', 'HeaderLabel_' + digest))
        self.participant_labels.setdefault(identity, label)  # memory only
        self.participant_spellings[identity].add(' '.join(name.split()))  # as written (case kept), memory only
        return identity


def never_merge_pairs():
    path = ROOT / 'config/identity_merges.json'
    if not path.exists():
        return []
    config = json.loads(path.read_text())
    pairs = list(config.get('never_merge', []))
    for item in config.get('kept_separate', []):
        pairs.append([f'kg:{i}' if isinstance(i, int) else i for i in item['ids']])
    return pairs


def reviewed_label_identity(key, label):
    """Pseudonymous participant ID for a reviewed label that no header spells ("Michelle
    (Regal Domestics)"): a person split off a bare name in config/document_attributions.json."""
    return 'participant_' + hmac.new(key, ('reviewed_label\0' + label).encode(), hashlib.sha256).hexdigest()[:24]


def attribution_items(key):
    """config/document_attributions.json as [(item, owner identity)]: an owner is an existing
    identity (canonical) or a new reviewed label (label). The one reader of that file."""
    path = ROOT / 'config/document_attributions.json'
    items = json.loads(path.read_text())['attributions'] if path.exists() else ()
    return [(item, reviewed_label_identity(key, item['label']) if item.get('label') else item['canonical']) for item in items]


def reviewed_labels():
    """Labels of new people named in config/document_attributions.json ("label" owners)."""
    return sorted({item['label'] for item, _ in attribution_items(b'') if item.get('label')})


def document_attributions(key):
    """{(bare label identity, efta): owner} from config/document_attributions.json: reviewed
    owners of a bare name in specific documents."""
    found = {}
    for item, owner in attribution_items(key):
        for efta in item['eftas']:
            if found.setdefault((item['member'], efta), owner) != owner:
                raise ValueError(f"document_attributions: {efta} has two owners for {item['member']}")
    return found


def recipient_repairs():
    """{(emails_merged id, 'sent_to'|'cc_to'): [email IDs]} recovered from the official
    scans by scripts/reocr_fragments.py (config/recipient_repairs.json)."""
    path = ROOT / 'config/recipient_repairs.json'
    repairs = defaultdict(list)
    for item in json.loads(path.read_text())['repairs'] if path.exists() else ():
        if not item['identity'].startswith('email_'):
            raise ValueError(f"recipient repair {item['record']} must name an email_ ID")
        repairs[item['record'], {'to': 'sent_to', 'cc': 'cc_to'}[item['role']]].append(item['identity'])
    return repairs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--view-edges', type=int, default=1500)
    args = parser.parse_args()
    if args.view_edges < 1:
        parser.error('--view-edges must be positive')
    out = ROOT / 'output'
    key = contact_key()
    entities = json.loads((out / 'normalized_entities.json').read_text())
    registry_path = ROOT / 'data/raw/persons_registry.json'
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else []
    builder = Builder(key, entities, registry_aliases(entities, registry))
    extracted_path = ROOT / 'data/raw/extracted_entities_filtered.json'
    raw_contacts = json.loads(extracted_path.read_text()).get('emails', ()) if extracted_path.exists() else ()
    # These contacts may never occur in parsed headers. Keep their HMAC IDs so
    # build_contacts can apply the same address repairs to its own retained nodes.
    raw_addresses = extracted_contact_addresses(raw_contacts, key)
    repairs = recipient_repairs()
    temporary = out / 'communication_graph.partial.db'
    temporary.unlink(missing_ok=True)
    db = sqlite3.connect(temporary)
    db.executescript('''
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE nodes(id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, label TEXT NOT NULL);
        CREATE TABLE evidence(source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            relationship_type TEXT NOT NULL, efta_number TEXT, message_index INTEGER,
            source_db TEXT NOT NULL, source_table TEXT NOT NULL, source_record_id INTEGER NOT NULL,
            date_sent TEXT);
    ''')
    stats = Counter()
    batch = []

    def flush():
        if batch:
            db.executemany('INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?)', batch)
            db.commit()
            batch.clear()

    pages = corpus_source.connect() if corpus_source.PATH.exists() else None
    header_cache = HeaderCache(out / '.build/header_checks.db', key,
                               [corpus_source.stamp() if pages else None,
                                *[hashlib.blake2b(Path(p).read_bytes()).hexdigest() for p in
                                  (header_text.__file__, corpus_source.__file__, corpus_adapter.__file__)]])
    plain = lru_cache(maxsize=1 << 17)(plain_text)

    def read_document(efta):
        return '\n'.join(t or '' for t, in pages.execute('SELECT text FROM pages WHERE doc_id=? ORDER BY page', (efta,))) if pages else ''

    prefetch = DocumentPrefetch(corpus_source.connect, read_document,
                                pages is not None and not header_cache.results)

    @lru_cache(maxsize=64)
    def header_blocks(efta):
        text, prepared = prefetch.read(efta)
        return text, {check: prepared[check.__name__] for check in
                      (header_block_text, body_after_bare_field, nonparty_header_text)
                      if getattr(check, '__name__', None) in prepared}

    def header_check(efta, check):
        text, blocks = header_blocks(efta)
        if check not in blocks:
            blocks[check] = check(text) if text else (None if check is header_block_text else '')
        return blocks[check]

    @lru_cache(maxsize=1 << 17)
    def name_variant(name):
        return ocr_name_variant(name, reference_names)

    @lru_cache(maxsize=1 << 17)
    def exempt_name(name):
        # Identity/review inputs are fixed during this build. Re-evaluate them
        # in every new build, independently of the persisted document checks.
        return body_name_exempt(name, builder, addressed_names, name_variant)

    @lru_cache(maxsize=32768)
    def outside_header(efta, name):
        def compute():
            blocks = header_check(efta, header_block_text)
            return blocks is not None and word_key(name) not in blocks
        return bool(header_cache.get(efta, name, compute))

    def body_line(efta, name):
        """Plain text, unreviewed and unknown, not an OCR form of a frequent name, and in none of
        the header blocks of an available document."""
        if not plain(name):
            return False
        return outside_header(efta, name)

    @lru_cache(maxsize=32768)
    def after_bare_field(efta, name):
        return bool(header_cache.get(efta, ('after_bare_field', name),
                                     lambda: word_key(name) in header_check(efta, body_after_bare_field)))

    @lru_cache(maxsize=32768)
    def nonparty_text(efta, name):
        return bool(header_cache.get(efta, ('nonparty_text', name),
                                     lambda: word_key(name) in header_check(efta, nonparty_header_text)))

    source = email_source.headers()
    query = '''SELECT id,doc_id,message_index,sender_name,sender_email,to_json,
                      cc_json,date FROM headers WHERE noise=0'''
    wrapped_cache = {}  # recipient columns recur across millions of records
    addressed_names = set()
    name_counts = counted_header_names(source, addressed_names=addressed_names)
    full_names = wrapped_full_names(counts=name_counts)
    reference_names = frequent_names(counts=name_counts)
    del name_counts

    def needs_document(row):
        _, _, _, name, address, to_raw, cc_raw, _ = row
        name, address = parse_sender(name, address)
        names = {name} if not valid_email(address) and isinstance(name, str) and not exempt_name(name) else set()
        names.update(recipient for raw in (to_raw, cc_raw) for recipient, email in recipient_fields(raw) or ()
                     if not email and isinstance(recipient, str) and not exempt_name(recipient)
                     and not signed_own_name(recipient, email, name))
        return names

    with prefetch:
        for record_id, efta, index, sender_name, sender_email, to_raw, cc_raw, date in prefetch.rows(source.execute(query), needs_document):
            stats['non_noise_message_records'] += 1
            if not valid_email(sender_email) and (recovered := parse_sender(sender_name, sender_email)) != (sender_name, sender_email):
                sender_name, sender_email = recovered
                stats['sender_address_recovered_from_header'] += 1
            # A sender with an address elsewhere is a real mailbox ("Seamless", "Popular in your network").
            if (not valid_email(sender_email) and isinstance(sender_name, str)
                    and not exempt_name(sender_name)):
                if after_bare_field(efta, sender_name):
                    stats['body_text_after_bare_field_dropped'] += 1
                    builder.mark_nonparty(sender_name)
                    sender_name = None
                elif nonparty_text(efta, sender_name):
                    stats['nonparty_header_text_dropped'] += 1
                    builder.mark_nonparty(sender_name)
                    sender_name = None
                elif body_line(efta, sender_name):
                    stats['body_text_senders_dropped'] += 1
                    builder.mark_nonparty(sender_name)
                    sender_name = None
            sender = builder.endpoint(sender_name, sender_email)
            day = normalized_date(date)
            if date and day is None:
                stats['rejected_nonempty_date_records'] += 1
            if sender is None:
                stats['missing_usable_sender'] += 1
            linked = False
            recipients = set()
            seen = set()
            associations = set()
            known_sender = builder.known_name(sender_name)
            if sender and sender.startswith('email_'):
                associations.add(builder.association(known_sender, sender_name, sender))
            for role, raw in [('sent_to', to_raw), ('cc_to', cc_raw)]:
                fields = recipient_fields(raw)
                if fields is None:
                    stats['invalid_recipient_json_fields'] += 1
                    continue
                joined = wrapped_cache.get(fields)
                if joined is None:
                    if len(wrapped_cache) >= 1 << 17:
                        wrapped_cache.clear()
                    joined = wrapped_cache[fields] = join_wrapped_names(fields, full_names)
                stats['wrapped_names_rejoined'] += len(fields) - len(joined)
                fields = joined
                for name, address in fields:
                    if signed_own_name(name, address, sender_name):
                        stats['sender_signoff_recipients_dropped'] += 1
                        continue
                    if (not address and isinstance(name, str)
                            and not exempt_name(name)):
                        if after_bare_field(efta, name):
                            stats['body_text_after_bare_field_dropped'] += 1
                            builder.mark_nonparty(name)
                            continue
                        if nonparty_text(efta, name):
                            stats['nonparty_header_text_dropped'] += 1
                            builder.mark_nonparty(name)
                            continue
                        if body_line(efta, name):
                            stats['body_text_recipients_dropped'] += 1
                            builder.mark_nonparty(name)
                            continue
                    recipient = builder.endpoint(name, address)
                    if recipient is None:
                        continue
                    recipients.add(recipient)
                    known_recipient = builder.known_name(name)
                    if recipient.startswith('email_'):
                        associations.add(builder.association(known_recipient, name, recipient))
                    if sender is None or (recipient, role) in seen:
                        continue
                    seen.add((recipient, role))
                    batch.append((sender, recipient, role, efta, index, *email_source.HEADERS_SOURCE, record_id, day))
                    linked = True
                for recipient in repairs.get((record_id, role), ()):
                    builder.nodes.setdefault(recipient, (recipient, 'email', 'Email_' + recipient.split('_', 1)[1]))
                    recipients.add(recipient)
                    stats['recipients_recovered_from_scans'] += 1
                    if sender is None or (recipient, role) in seen:
                        continue
                    seen.add((recipient, role))
                    batch.append((sender, recipient, role, efta, index, *email_source.HEADERS_SOURCE, record_id, day))
                    linked = True
            associations.discard(None)
            for identity, email_id, kind in sorted(associations):  # sets iterate in hash-seed order
                batch.append((identity, email_id, kind, efta, index, *email_source.HEADERS_SOURCE, record_id, day))
                stats[kind + '_records'] += 1
            if not recipients:
                stats['missing_usable_recipients'] += 1
            if linked:
                stats['message_records_with_edges'] += 1
            if len(batch) >= 10000:
                flush()
            if stats['non_noise_message_records'] % 250000 == 0:
                print('Processed', stats['non_noise_message_records'], 'message records', flush=True)
    source.close()
    flush()
    outside_header.cache_clear()
    after_bare_field.cache_clear()
    nonparty_text.cache_clear()
    header_cache.close()
    header_blocks.cache_clear()
    if pages is not None:
        pages.close()
    # Backfill ONLY directed pair/types absent from the merged metadata layer.
    # Overlapping source messages cannot be safely deduplicated, so do not add
    # their counts to existing merged pairs. Preserve supplemental provenance.
    existing_pairs = set(db.execute("SELECT DISTINCT source_id,target_id,relationship_type FROM evidence WHERE relationship_type IN ('sent_to','cc_to')"))
    source = email_source.parties()
    query = '''SELECT e.id,e.doc_id,e.date,a.address,b.address,b.role
               FROM messages e JOIN parties a ON a.message_id=e.id
               JOIN parties b ON b.message_id=e.id
               WHERE e.noise=0 AND a.role='from' AND b.role IN ('to','cc')
               AND a.address IS NOT NULL AND b.address IS NOT NULL'''
    supplemented_pairs, supplemented_records, seen_source_pairs = set(), set(), set()
    for record_id, efta, date, sender_address, recipient_address, role in source.execute(query):
        sender_email, recipient_email = valid_email(sender_address), valid_email(recipient_address)
        if not sender_email or not recipient_email:
            continue
        a = contact_id(sender_email, 'email', key)
        b = contact_id(recipient_email, 'email', key)
        kind = 'sent_to' if role == 'to' else 'cc_to'
        pair = (a, b, kind)
        if pair in existing_pairs or (record_id, pair) in seen_source_pairs:
            continue
        seen_source_pairs.add((record_id, pair))
        supplemented_pairs.add(pair)
        supplemented_records.add(record_id)
        builder.endpoint(None, sender_email)
        builder.endpoint(None, recipient_email)
        batch.append((a, b, kind, efta, None, *email_source.PARTIES_SOURCE, record_id, normalized_date(date)))
        stats['supplemental_pair_evidence_records'] += 1
        if len(batch) >= 10000:
            flush()
    flush()
    source.close()
    stats['supplemental_new_pair_types'] = len(supplemented_pairs)
    stats['supplemental_source_message_records'] = len(supplemented_records)
    # Published external dumps (e.g. the DDoSecrets Yahoo tranche), deduplicated
    # by Message-ID. Labelled per source so leaked material stays distinguishable.
    seen_message_ids = set()
    for source_name, relative, index, message_id, sender_raw, to_list, cc_list, date in email_source.external():
        if message_id and message_id in seen_message_ids:
            stats['external_duplicate_message_ids'] += 1
            continue
        seen_message_ids.add(message_id)
        stats[f'external_records:{source_name}'] += 1
        senders = parse_recipient(sender_raw)
        sender = builder.endpoint(*senders[0]) if senders else None
        linked, seen = False, set()
        for role, values in (('sent_to', to_list), ('cc_to', cc_list)):
            for value in values:
                for name, address in parse_recipient(value):
                    recipient = builder.endpoint(name, address)
                    if sender is None or recipient is None or (recipient, role) in seen:
                        continue
                    seen.add((recipient, role))
                    batch.append((sender, recipient, role, None, None, f'external:{source_name}', relative, index, date))
                    linked = True
        stats[f'external_records_with_edges:{source_name}'] += linked
        if len(batch) >= 10000:
            flush()
    flush()
    # Explicit sender/recipient email + upstream resolved identity association.
    # Kept separate from sender->recipient edges; not asserted account ownership.
    source = email_source.parties()
    # Upstream resolution, not the exported KG IDs: require a non-fuzzy canonical match.
    query = '''SELECT p.address,r.name,p.message_id,e.doc_id,e.date,p.role
               FROM parties p JOIN messages e ON e.id=p.message_id
               JOIN identities r ON r.id=p.identity_id
               WHERE e.noise=0 AND p.address IS NOT NULL
               AND r.confidence>=0.9 AND r.method IN ('exact','email','manual')'''
    seen_identity = set()
    for address, canonical_name, record_id, efta, date, role in source.execute(query):
        known_id = builder.known_name(canonical_name)
        if known_id is None or not valid_email(address):
            continue
        email_id = builder.endpoint(None, address)
        identity = builder.entity_identity(known_id)
        # One record per message, even when the address is in both From and To.
        if (record_id, identity, email_id) in seen_identity:
            continue
        seen_identity.add((record_id, identity, email_id))
        batch.append((identity, email_id, 'upstream_resolved_header_identity', efta, None,
                      *email_source.PARTIES_SOURCE, record_id, normalized_date(date)))
        stats['upstream_identity_association_records'] += 1
        if len(batch) >= 10000:
            flush()
    source.close()
    flush()
    db.executemany('INSERT INTO nodes VALUES (?,?,?)', builder.nodes.values())
    db.commit()
    print('Resolving identities (reviewed merges, OCR variants, single-person addresses)', flush=True)
    for identity, value in raw_addresses.items():
        builder.email_values.setdefault(identity, value)
    merges = resolve_identities(db, builder.email_values, load_identity_merges(), never_merge_pairs(),
                                builder.participant_labels, builder.matcher,
                                {f'kg:{i}': e['name'] for i, e in builder.entities.items()},
                                {f'kg:{i}': e['name'] for i, e in builder.entities.items()
                                 if e['entity_type'] == 'person' and not e.get('suspected_parse_artifact')},
                                builder.participant_spellings, context_corpus=True)
    db.execute('CREATE TABLE identity_map(member_id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL, rule TEXT NOT NULL, confidence TEXT NOT NULL)')
    db.executemany('INSERT INTO identity_map VALUES (?,?,?,?)', [(m, c, r, k) for m, (c, r, k) in merges.items()])
    stats['identity_merges'] = dict(Counter(rule.split(':')[0] for _, rule, _ in merges.values()))
    stats['header_labels_dropped_as_non_names'] = builder.dropped_labels
    stats['garbled_header_names_recovered'] = builder.recovered_labels
    print('Aggregating directed pairs and building indexes', flush=True)
    # evidence keeps raw endpoint IDs; source/target in edges are canonical.
    db.executescript('''
        ALTER TABLE evidence ADD COLUMN canonical_source TEXT;
        ALTER TABLE evidence ADD COLUMN canonical_target TEXT;
        UPDATE evidence SET
            canonical_source=coalesce((SELECT canonical_id FROM identity_map WHERE member_id=source_id), source_id),
            canonical_target=coalesce((SELECT canonical_id FROM identity_map WHERE member_id=target_id), target_id);
    ''')
    print('Attributing bare first names from their documents', flush=True)
    def final(identity):
        seen = set()
        while identity in merges and merges[identity][0] != identity and identity not in seen:
            seen.add(identity)
            identity = merges[identity][0]
        return identity
    attested = {i for i, in db.execute("SELECT DISTINCT canonical_source FROM evidence WHERE relationship_type IN ('header_name_association', 'header_label_association')")}
    labels = {i: label for i, label in builder.participant_labels.items() if final(i) == i and (
        bare_first_name(label, builder.review.get(i, {}).get('verdict')) or couple_words(label.split()) or context_person(label, builder.review.get(i, {}).get('verdict'), i in attested or label in full_names))}
    labels.update({f'kg:{i}': e['name'] for i, e in builder.entities.items() if e['entity_type'] == 'person'})
    email_ids = {value.casefold(): identity for identity, value in builder.email_values.items()}
    corpus = corpus_source.connect() if corpus_source.PATH.exists() else None
    def document_text(efta):
        if corpus is None:
            return ''
        pages = corpus.execute('SELECT text FROM pages WHERE doc_id=? LIMIT ?', (efta, MAX_CONTEXT_PAGES + 1)).fetchall()
        return '' if len(pages) > MAX_CONTEXT_PAGES else '\n'.join(t or '' for t, in pages)
    rows = db.execute("SELECT rowid,efta_number,canonical_source,canonical_target FROM evidence WHERE relationship_type IN ('sent_to','cc_to')").fetchall()
    owners = first_name_owners(rows, labels, final, email_ids, document_text)
    how = dict.fromkeys(owners, 'document_names_one_person')
    reviewed = document_attributions(key)
    for label in reviewed_labels():
        identity = reviewed_label_identity(key, label)
        builder.nodes.setdefault(identity, (identity, 'participant', 'HeaderLabel_' + identity.split('_', 1)[1]))
        builder.participant_labels.setdefault(identity, name_key(label))
    db.executemany('INSERT OR IGNORE INTO nodes VALUES (?,?,?)', [builder.nodes[reviewed_label_identity(key, label)] for label in reviewed_labels()])
    for row_id, efta, source, target in rows:
        for side, identity in (('source', source), ('target', target)):
            owner = reviewed.get((final(identity), efta))
            # Also when the other side is the owner: "Bob" signing Robert Trivers's mail is one person.
            if owner is not None:
                owners[row_id, side] = final(owner)
                how[row_id, side] = 'reviewed_document_attribution'
                stats['reviewed_document_attributions'] += 1
    # Per node: records moved in from (or out of) a bare name, for the viewer's
    # "merged / separated" panel (identity_review.json, written with identity_map.json).
    moved = defaultdict(lambda: [0, []])
    for row_id, efta, source, target in rows:
        for side, before in (('source', source), ('target', target)):
            if (row_id, side) in owners:
                item = moved[before, owners[row_id, side], how[row_id, side]]
                item[0] += 1
                if efta and len(item[1]) < 10 and efta not in item[1]:
                    item[1].append(efta)
    if corpus is not None:
        corpus.close()
    db.executemany('UPDATE evidence SET canonical_source=? WHERE rowid=?', [(o, r) for (r, side), o in owners.items() if side == 'source'])
    db.executemany('UPDATE evidence SET canonical_target=? WHERE rowid=?', [(o, r) for (r, side), o in owners.items() if side == 'target'])
    # Which records moved, so node-level readers (sample_documents, node pages) follow the move too.
    db.execute('CREATE TABLE reattributions(evidence_rowid INTEGER, side TEXT, owner TEXT, PRIMARY KEY(evidence_rowid, side))')
    db.executemany('INSERT INTO reattributions VALUES (?,?,?)', [(r, side, o) for (r, side), o in owners.items()])
    db.execute('CREATE INDEX reattributions_owner ON reattributions(owner)')
    stats['bare_first_names_attributed_from_document'] = len(owners)
    db.executescript('''
        CREATE INDEX evidence_pair ON evidence(canonical_source,canonical_target,relationship_type);
        CREATE INDEX evidence_raw_pair ON evidence(source_id,target_id,relationship_type);
        CREATE TABLE edges AS SELECT canonical_source AS source_id,canonical_target AS target_id,relationship_type,
            count(*) AS record_count,min(date_sent) AS date_first,max(date_sent) AS date_last
            FROM evidence
            -- an identity link absorbed by a merge is no longer an edge
            WHERE relationship_type!='header_name_association' AND NOT (canonical_source=canonical_target AND relationship_type IN ('header_label_association','upstream_resolved_header_identity'))
            GROUP BY canonical_source,canonical_target,relationship_type;
        CREATE UNIQUE INDEX edges_pair ON edges(source_id,target_id,relationship_type);
        CREATE INDEX edges_count ON edges(record_count DESC);
        CREATE TABLE node_degrees AS SELECT id,count(*) AS unique_neighbors FROM (
            SELECT source_id AS id,target_id AS neighbor FROM edges WHERE source_id!=target_id
            UNION SELECT target_id,source_id FROM edges WHERE source_id!=target_id
        ) GROUP BY id;
    ''')
    stats['nodes'] = db.execute('SELECT count(*) FROM nodes').fetchone()[0]
    stats['directed_pair_edges'] = db.execute('SELECT count(*) FROM edges').fetchone()[0]
    stats['evidence_records'] = db.execute('SELECT count(*) FROM evidence').fetchone()[0]
    # Bound offline HTML size; full graph and complete reference evidence remain in SQLite.
    view_pairs = db.execute('''SELECT source_id,target_id,relationship_type,record_count,date_first,date_last
                              FROM edges WHERE relationship_type IN ('sent_to','cc_to')
                              ORDER BY record_count DESC,source_id,target_id,relationship_type LIMIT ?''', (args.view_edges,)).fetchall()
    visible_ids = {identity for row in view_pairs for identity in row[:2]}
    compounds = resolved_header_compounds(
        {**builder.participant_labels, **{f'kg:{i}': e['name'] for i, e in builder.entities.items()
                                         if e['entity_type'] == 'person'}}, final)
    db.execute('CREATE TABLE compound_labels(id TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE compound_members(compound_id TEXT,person_id TEXT,PRIMARY KEY(compound_id,person_id))')
    db.executemany('INSERT INTO compound_labels VALUES (?)', [(i,) for i in compounds])
    db.executemany('INSERT INTO compound_members VALUES (?,?)', [(i, p) for i, people in compounds.items() for p in sorted(people)])
    stats['compound_header_labels'] = len(compounds)
    stats['compound_constituent_links'] = sum(map(len, compounds.values()))
    # Include identity links for email nodes appearing in the selected communication slice.
    identity_pairs = db.execute("SELECT source_id,target_id,relationship_type,record_count,date_first,date_last FROM edges WHERE relationship_type IN ('upstream_resolved_header_identity','header_label_association')").fetchall()
    for row in identity_pairs:
        if row[1] in visible_ids:
            view_pairs.append(row)
            visible_ids.add(row[0])
    visible_compounds = {i: people for i, people in compounds.items() if i in visible_ids}
    visible_ids.update(p for people in visible_compounds.values() for p in people)
    degrees = dict(db.execute('SELECT id,unique_neighbors FROM node_degrees'))
    def view_id(identity):
        return int(identity[3:]) if identity.startswith('kg:') else identity
    # Where each node and merged member was read, so OCR damage can be checked
    # against (and corrected from) the official scan.
    # efta_number in the index lets sample_documents (and node_documents.py)
    # read each endpoint's documents in order without sorting its evidence.
    db.execute('CREATE INDEX evidence_raw_target ON evidence(target_id,efta_number)')
    efta_of = {}
    for identity in sorted(set(merges) | visible_ids):
        efta_of[identity] = sample_documents(db, identity)
    urls = document_urls({e for found in efta_of.values() for e in found})
    def provenance(identity):
        artifacts = ocr_artifacts(builder.email_values[identity]) if identity in builder.email_values else []
        return {'source_documents': [{'efta_number': e, **({'pdf_url': urls[e]} if e in urls else {})} for e in efta_of.get(identity, [])],
                **({'ocr_artifacts': artifacts} if artifacts else {})}
    absorbed = defaultdict(list)
    for member, (canonical, rule, confidence) in merges.items():
        absorbed[canonical].append({'id': member, 'rule': rule, 'confidence': confidence, **provenance(member)})
    record_counts = Counter()
    for column in ('source_id', 'target_id'):
        record_counts.update(dict(db.execute(f"SELECT {column}, count(*) FROM evidence GROUP BY {column}")))
    review_out = defaultdict(lambda: {'reattributed_in': [], 'reattributed_out': [], 'kept_apart': [], 'merged_records': {}})
    for (before, after, rule), (n, eftas) in sorted(moved.items(), key=lambda item: -item[1][0]):
        review_out[view_id(after)]['reattributed_in'].append({'id': view_id(before), 'rule': rule, 'records': n, 'documents': eftas})
        review_out[view_id(before)]['reattributed_out'].append({'id': view_id(after), 'rule': rule, 'records': n, 'documents': eftas})
    for pair in never_merge_pairs():
        a, b = (f'kg:{i}' if isinstance(i, int) else i for i in pair)
        for x, y in ((a, b), (b, a)):
            review_out[view_id(merges.get(x, (x,))[0])]['kept_apart'].append({'id': view_id(y), 'canonical': view_id(merges.get(y, (y,))[0]), 'member': view_id(x)})
    for member, (canonical, _, _) in merges.items():
        review_out[view_id(canonical)]['merged_records'][str(view_id(member))] = record_counts[member]
    (out / 'identity_review.json').write_text(json.dumps({str(k): v for k, v in review_out.items()}, indent=1) + '\n')
    # Pseudonymous map for build_contacts (entity-export contacts) and the viewer.
    (out / 'identity_map.json').write_text(json.dumps(
        {member: {'canonical': view_id(c), 'rule': r, 'confidence': k, **provenance(member)} for member, (c, r, k) in sorted(merges.items())}, indent=1) + '\n')
    review = node_review()['participants']
    members = defaultdict(list)
    for member, (canonical, _, _) in merges.items():
        members[canonical].append(member)
    person_words = {w for e in builder.entities.values() if e['entity_type'] == 'person'
                    for name in (e['name'], *e.get('original_names', [])) for w in re.findall(r"[^\W\d_]+", name.casefold())
                    if w not in dictionary_words()[2]}  # the KG holds "Secret Service", "Bodyguard"
    def classify(identity):
        """(entity_type, label_quality) a header identity is drawn with."""
        _, kind, _ = builder.nodes[identity]
        quality = (review.get(identity, {}).get('verdict') if identity in review
                   else label_quality(builder.participant_labels[identity]) if identity in builder.participant_labels else 'ok')
        if (identity in builder.nonparty_ids and identity not in builder.party_ids
                or identity in builder.participant_labels and header_junk_reason(builder.participant_labels[identity])):
            quality = 'not_a_name'
        if quality == 'ok' and identity in builder.participant_labels and all(
                m in builder.participant_labels and m not in review for m in (identity, *members[identity])) and plain_words(
                builder.participant_spellings[identity], person_words) and not any(
                not plain_text(s, person_words) and name_word(w, person_words) for m in members[identity]
                for s in builder.participant_spellings[m] for w in re.findall(r"[^\W\d_]{2,}", s)):
            quality = 'not_a_name'  # merged OCR variants ("i nteger") may be anything but a name word
        # Reviews are private input too: only fixed quality codes may be exported.
        if quality not in {'ok', 'ok_person', 'ok_entity', 'group_or_role', 'uncertain', 'not_a_name',
                           'variant_of_known', 'fix_label', 'drop', 'place', *DROP_VERDICTS}:
            quality = 'uncertain'
        if kind == 'participant' and quality == 'ok_entity':
            kind = 'organization'  # a company or office name from the headers (node review), not a person
        elif kind == 'participant' and quality == 'place':
            kind, quality = 'location', 'ok'  # a header phrase naming a place (node review)
        if kind == 'participant' and BARE_DOMAIN.fullmatch(builder.participant_labels.get(identity, '')):
            kind = 'organization'  # "Vegas.com": a company's sender name
        if kind == 'email' and non_mailbox_reason(builder.email_values.get(identity, '')):
            quality = 'not_a_name'
        if kind == 'email' and automated_company(builder.email_values.get(identity, '')):
            kind = 'organization'  # a company's automated sender (automated_company_sender)
        return kind, quality
    # Every canonical header identity, so nodes pulled in later (viewer "Expand") are drawn the same way.
    db.execute('CREATE TABLE node_classes(id TEXT PRIMARY KEY, entity_type TEXT, label_quality TEXT)')
    db.executemany('INSERT INTO node_classes VALUES (?,?,?)',
                   [(i, *classify(i)) for i in builder.nodes if not i.startswith('kg:') and final(i) == i])
    view_nodes = []
    for identity in sorted(visible_ids):
        if identity.startswith('kg:'):
            continue  # Existing normalized KG nodes already carry their original records.
        if identity.startswith('company_'):
            continue  # A header name merged into a company-layer node (build_companies; joined in build_contacts).
        _, _, label = builder.nodes[identity]
        kind, quality = classify(identity)
        view_nodes.append({'id': identity, 'name': label, 'entity_type': kind,
                           **({'compound': True, 'compound_members': [view_id(p) for p in sorted(compounds[identity])]}
                              if identity in compounds else {}),
                           **({'label_quality': quality} if quality not in ('ok', 'ok_person', 'ok_entity') else {}),
                           'full_metadata_unique_connections': degrees.get(identity, 0),
                           'source': 'Structured email headers; pseudonymous value, not verified identity',
                           **provenance(identity),
                           **({'merged_identities': absorbed[identity]} if absorbed.get(identity) else {})})
    # The raw extracted-contact layer can contain transport IDs which the header
    # parser never admitted. Its small export needs the same quality overlays.
    if raw_contacts:
        present = {n['id'] for n in view_nodes}
        for node in non_mailbox_contact_nodes(raw_contacts, key):
            if final(node['id']) == node['id']:
                db.execute('INSERT OR REPLACE INTO node_classes VALUES (?,?,?)',
                           (node['id'], 'email', 'not_a_name'))
                if node['id'] not in present:
                    view_nodes.append(node)
    view_edges = []
    for a, b, kind, count, first, last in view_pairs:
        refs = sample_references(db, a, b, kind)
        meaning = ('Upstream resolved canonical name matched exactly to graph label; resolver confidence >=0.9 and non-fuzzy method. Identity unverified, not confirmed ownership.'
                   if kind == 'upstream_resolved_header_identity' else
                   'Matching name label and address supplied together in a parsed header; label identity unverified, not confirmed ownership.'
                   if kind == 'header_label_association' else
                   'Parsed sender to recipient header relation; extracted message records, not unique real-world emails. Header-label identity may be unresolved.')
        edge_id = 'message_' + hashlib.sha256(json.dumps([a, b, kind]).encode()).hexdigest()[:24]
        view_edges.append({'id': edge_id, 'source_entity_id': view_id(a), 'target_entity_id': view_id(b),
                           'relationship_type': kind, 'weight': count, 'date_first': first, 'date_last': last,
                           'metadata': {'meaning': meaning, 'record_count': count,
                                        'sample_source_references': [dict(zip(['efta_number','source_db','source_table','source_record_id','message_index'], r)) for r in refs],
                                        'complete_evidence': 'output/communication_graph.db'}})
    for compound, people in sorted(visible_compounds.items()):
        for person in sorted(people):
            view_edges.append({'id': 'compound_' + hashlib.sha256(json.dumps([compound, person]).encode()).hexdigest()[:24],
                               'source_entity_id': view_id(compound), 'target_entity_id': view_id(person),
                               'relationship_type': 'compound_member', 'weight': 1,
                               'metadata': {'meaning': 'This retained header label names this person as one of its constituents; not an identity merge.',
                                            'sample_source_references': provenance(compound)['source_documents'],
                                            'complete_evidence': 'output/communication_graph.db:compound_members'}})
    summary = dict(stats)
    summary['nodes_with_edges'] = db.execute('SELECT count(*) FROM nodes WHERE id IN (SELECT source_id FROM edges UNION SELECT target_id FROM edges)').fetchone()[0]
    summary.update(view_pair_limit=args.view_edges, view_nodes=len(view_nodes), view_edges=len(view_edges),
                   node_types=dict(Counter(row[1] for row in builder.nodes.values())),
                   caveats=['Uses non-noise emails_merged plus communications.db pair/types absent from merged; overlapping pair counts are not added together.',
                            'Supplementary communications.db edges preserve source evidence but do not repair missing messages/counts for pairs already represented in merged.',
                            'Merged recipient strings and structured [name,email] arrays are parsed; malformed/unresolved tokens remain incomplete.',
                            'Repeated emails across documents are not deduplicated. Counts describe source records.',
                            'Phones and body mentions are not inferred from email headers.',
                            'Unknown name-only participants use HeaderLabel pseudonyms, not email or person claims.',
                            'Viewer is a highest-record-count slice, not the full network. Full graph/evidence is in communication_graph.db.',
                            'Structured metadata can contain OCR, parsing, and identity-resolution errors.'])
    (out / 'communication_nodes.json').write_text(json.dumps(view_nodes, indent=2) + '\n')
    (out / 'communication_edges.json').write_text(json.dumps(view_edges, indent=2) + '\n')
    (out / 'communication_summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    db.commit()
    db.close()
    replace_if_changed(temporary, out / 'communication_graph.db')
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
