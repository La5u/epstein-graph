"""PRIVATE, local-only: reveal real emails, phone numbers and header names for review.

    .venv/bin/python scripts/private_contacts.py

Writes ONLY under data/private/ (git-ignored, never part of output/):
  contact_lookup.json      pseudonym ID -> real value, raw spellings, occurrence count
                           (emails, phones and HeaderLabel participant names)
  contact_duplicates.csv   possible duplicate groups with real values side by side
  viewer_secret.json       real values + document pages for the viewer; visualize_graph.py
                           embeds them encrypted (scripts/viewer_secret.py)
  node_docs/               per-node document lists linked from it (scripts/node_documents.py)

Duplicate groups are review candidates, never merged automatically. Do not
share these files: they contain personal contact details from the corpus.
"""
import csv
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from functools import lru_cache
from collections import Counter, defaultdict
from pathlib import Path

from audit_normalization import collect_contacts
import emails as email_source
from build_communications import Builder, parse_recipient, parse_sender, attribution_items, reviewed_label_identity, reviewed_labels, valid_email
from build_contacts import contact_id, contact_key, normalize_contact
from viewer_secret import write_secret
from node_documents import write_pages
from derived_files import metadata_matches, remember_metadata
from header_context_resolution import decorated_name

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / 'data/private'
GMAIL = {'gmail.com', 'googlemail.com'}


def email_keys(value):
    """(rule, key) pairs; contacts sharing a key are possible duplicates."""
    local, domain = value.rsplit('@', 1)
    domain = domain.casefold()
    yield 'email: same address ignoring case', value.casefold()
    base = local.split('+', 1)[0].casefold()
    if domain in GMAIL:
        yield 'email: Gmail ignores dots and +tags', base.replace('.', '') + '@gmail.com'
    elif '+' in local:
        yield 'email: same address without +tag', base + '@' + domain
    try:
        yield 'email: same punycode domain', local.casefold() + '@' + domain.encode('idna').decode('ascii')
    except UnicodeError:
        pass


def phone_keys(value):
    digits = value.lstrip('+')
    yield 'phone: same digits with/without +', digits
    if len(digits) == 11 and digits.startswith('1'):
        yield 'phone: US number with/without leading 1 (assumed)', digits[1:]
    elif len(digits) == 10:
        yield 'phone: US number with/without leading 1 (assumed)', digits
    if len(digits) > 10:
        yield 'phone: same last 10 digits (international/trunk prefix?)', digits[-10:]


def name_keys(value):
    words = re.findall(r'[^\W\d_]+', value.casefold())
    if len(words) >= 2:
        yield 'name: same words ignoring order/punctuation', ' '.join(sorted(words))


def collect_participants(key, external=None):
    """HeaderLabel pseudonym -> raw header-name spellings, replaying the importer.

    Builder without graph entities turns every acceptable name-only header
    label into exactly the participant ID that build_communications produced.
    """
    builder, labels, names = Builder(key, []), defaultdict(Counter), Counter()

    @lru_cache(maxsize=1 << 17)
    def recipients(raw_value):  # identical recipient columns recur across millions of records
        try:
            items = json.loads(raw_value or '[]')
        except (ValueError, TypeError):
            return ()
        return tuple(recipient for item in (items if isinstance(items, list) else ())
                     for recipient, _ in parse_recipient(item) if recipient)

    with closing(email_source.headers()) as db:
        # Group repeated headers, but keep first-seen order: Counter's ties
        # decide which raw spelling is shown. A later occurrence of the same
        # group cannot introduce a new name.
        for name, address, to_raw, cc_raw, count in db.execute(
                'SELECT sender_name,sender_email,to_json,cc_json,count(*) FROM headers WHERE noise=0 '
                'GROUP BY sender_name,sender_email,to_json,cc_json ORDER BY min(id)'):
            name, address = parse_sender(name, address)
            if isinstance(name, str):
                names[name] += count
            for raw in (to_raw, cc_raw):
                for recipient in recipients(raw):
                    names[recipient] += count
    for *_, sender, to_list, cc_list, _ in email_source.external() if external is None else external:
        for value in [sender, *to_list, *cc_list]:
            for name, address in parse_recipient(value):
                if not address and name:
                    names[name] += 1
    # Each distinct spelling resolved once, in first-seen order (most_common breaks ties by it).
    for name, count in names.items():
        identity = builder.endpoint(name, None)
        if identity and identity.startswith('participant_'):
            labels[identity][' '.join(name.split())] += count
    return labels


def lookup_stamp(key):
    """Inputs of build_lookup: the parsing code (project modules imported here), configs, raw header sources (size and
    mtime) and the contact key. HMAC so low-entropy inputs never reach the stamp."""
    import hashlib, hmac
    digest = hmac.new(key, digestmod=hashlib.sha256)
    scripts = {Path(m.__file__).resolve() for m in list(sys.modules.values()) if getattr(m, '__file__', None)}
    scripts = {path for path in scripts if path.parent == ROOT / 'scripts'}
    # Only these reviews affect raw header-name participation. Identity merges,
    # places, survivor policy, etc. are applied downstream; editing them must
    # not replay millions of unchanged headers just to recover the same lookup.
    configs = [ROOT / 'config' / name for name in ('node_review.json', 'document_attributions.json')]
    for path in sorted([*scripts, *configs]):
        digest.update(path.name.encode() + (path.read_bytes() if path.exists() else b'<missing>'))
    for path in (PRIVATE / 'entity_spellings.db', *email_source.stamp_paths()[:2], ROOT / 'data/raw/extracted_entities_filtered.json', *email_source.stamp_paths()[2:]):
        stat = path.stat() if path.exists() else None
        digest.update(repr((str(path), stat and (stat.st_size, stat.st_mtime_ns))).encode())
    return digest.hexdigest()


def build_lookup(key):
    """Cached in data/private/.lookup_cache.json (replaying 4M header entries takes a
    minute); rebuilt whenever lookup_stamp changes."""
    cache, stamp = PRIVATE / '.lookup_cache.json', lookup_stamp(key)
    metadata = ROOT / 'output/.build/private_lookup_stamp.json'
    cached = None
    try:
        text = cache.read_text()
        cached = json.loads(text)
        if cached.get('stamp') == stamp or metadata_matches(metadata, stamp, text):
            return cached['lookup']
    except (OSError, ValueError):
        pass
    lookup = replay_lookup(key)
    if cached is None or cached.get('lookup') != lookup:
        text = json.dumps({'stamp': stamp, 'lookup': lookup}, ensure_ascii=False)
        write_private(cache, text)
    remember_metadata(metadata, stamp, text)
    return lookup


def replay_lookup(key):
    # The slim Jmail dump has millions of rows but only thousands survive its
    # source filter. Reuse those headers rather than decompressing/parsing it
    # separately for contact counts and participant names.
    external = list(email_source.external())
    _, emails, phones = collect_contacts(external=external)
    lookup = {}
    for kind, values in (('email', emails), ('phone', phones)):
        for raw, count in values.items():
            value = normalize_contact(raw, kind)
            if value is None:
                continue
            identity = contact_id(value, kind, key)
            entry = lookup.setdefault(identity, {'kind': kind, 'value': value, 'occurrences': 0, 'raw_spellings': Counter()})
            entry['occurrences'] += count
            entry['raw_spellings'][raw] += count
    for identity, spellings in collect_participants(key, external=external).items():
        lookup[identity] = {'kind': 'participant', 'value': spellings.most_common(1)[0][0],
                            'occurrences': sum(spellings.values()), 'raw_spellings': spellings}
    for label in reviewed_labels():  # people split off a bare name by review (document_attributions.json)
        lookup.setdefault(reviewed_label_identity(key, label), {'kind': 'participant', 'value': label, 'occurrences': 0,
                                                                'raw_spellings': Counter({label: 1})})
    entity_spellings = PRIVATE / 'entity_spellings.db'
    if entity_spellings.exists():
        with closing(sqlite3.connect(f'file:{entity_spellings}?mode=ro', uri=True)) as db:
            for identity, spelling, count in db.execute('SELECT key,spelling,n FROM spellings ORDER BY n DESC'):
                entry = lookup.setdefault(identity, {'kind': 'participant', 'value': spelling, 'occurrences': 0, 'raw_spellings': Counter()})
                entry['raw_spellings'][spelling] += count
                entry['occurrences'] += count
    for entry in lookup.values():
        entry['raw_spellings'] = [s for s, _ in entry['raw_spellings'].most_common(5)]
    return lookup


def cleaned_header_labels(lookup, protected=()):
    """Private presentation repairs; raw labels and extracted nodes are retained.

    A bare decorated first name needs identity evidence to expand it; only a
    complete remaining name receives a presentation repair here.
    """
    from build_communications import (header_name_words, name_key, label_quality, dictionary_words,
                                      FUNCTION_WORDS, NON_NAME_WORDS, ROLE_TOKENS, COMPANY_WORDS, WEEKDAYS,
                                      NAME_PARTICLES)
    protected = {name_key(n) for n in protected}
    dictionary, proper, _ = dictionary_words()
    common = dictionary - proper
    given = set(json.loads((ROOT / 'config/entity_first_names.json').read_text()))
    forbidden = (FUNCTION_WORDS | NON_NAME_WORDS | ROLE_TOKENS | COMPANY_WORDS | WEEKDAYS) - NAME_PARTICLES
    repairs = {}
    for i, entry in lookup.items():
        if entry.get('kind') != 'participant':
            continue
        label = entry['value']
        core, rule = decorated_name(label)
        if rule == 'id_suffix' and not re.search(r'-(?:[ax]\b|\d{1,3}\b)|\[dead\]', label, re.I):
            continue  # other hyphen letters can be genuine middle initials
        words = header_name_words(core)
        if (rule and 2 <= len(words) <= 4 and len(words[0]) >= 2 and len(words[-1]) >= 2
                and all(w.isalpha() for w in words)
                and not set(words) & forbidden and label_quality(core) == 'ok'
                and (words[0] in given or len(words[0]) >= 3 and words[0] not in common)
                and name_key(label) not in protected and name_key(core) not in protected):
            repairs[i] = {'value': core, 'rule': rule}
    return repairs


def fuller_labels(lookup, identity_map):
    """{canonical id: label} where a merged participant's label has a truncated word that a
    member spells in full ("Gerald Bvenr" -> "Gerald G. Barton", "Gfom Fmarmi" -> "George
    Farkas"). Word for word, each canonical word must start the member's word (extra middle
    initials allowed, no added words: those are mostly OCR tails like "Imailto"); a completed
    word must be a capitalised name word seen in 3+ frequent unmerged labels, and the
    truncated form must not be one itself ("Peter Gnuve" never becomes "Peter Gdorri")."""
    words = lambda v: re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", v)
    members = defaultdict(list)
    for member, m in identity_map.items():
        if m.get('canonical') in lookup and member in lookup and lookup[member]['kind'] == 'participant':
            members[m['canonical']].append(member)
    known = Counter()  # words of frequent labels that were not merged into another node
    for identity, entry in lookup.items():
        if entry['kind'] == 'participant' and identity not in identity_map and entry['occurrences'] >= 5:
            known.update({w.casefold() for w in words(entry['value'])})
    proper = lambda w: re.fullmatch(r"[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)*|[A-Z]{2,}(?:[-'][A-Z]+)*", w) is not None

    def extends(short, long, own):
        a, b = words(short), words(long)
        if len(a) < 2 or len(b) > len(a) + 1 or [w.casefold() for w in a] == [w.casefold() for w in b]:
            return False
        completed, i = 0, 0
        for w in b:
            if i < len(a) and w.casefold() == a[i].casefold():
                i += 1
            elif (i < len(a) and w.casefold().startswith(a[i].casefold()) and len(a[i]) >= 3 and proper(w)
                  and known[w.casefold()] >= 3 and known[a[i].casefold()] <= own):
                completed, i = completed + 1, i + 1
            elif len(w) == 1 and 0 < i < len(a):
                continue  # middle initial
            else:
                return False
        return i == len(a) and completed > 0

    out = {}
    for canonical, ids in members.items():
        label = lookup[canonical]['value']
        own = int(lookup[canonical]['occurrences'] >= 5)  # its own label does not make a truncation a known word
        better = [lookup[i] for i in ids if extends(label, lookup[i]['value'], own)]
        if better:
            out[canonical] = max(better, key=lambda e: (e['occurrences'], e['value']))['value']
    return out

def duplicate_groups(lookup):
    groups = defaultdict(set)
    for identity, entry in lookup.items():
        keys = {'email': email_keys, 'phone': phone_keys, 'participant': name_keys}[entry['kind']](entry['value'])
        for rule, k in keys:
            groups[rule, k].add(identity)
    # Drop groups whose members exactly repeat a group already listed.
    seen, result = set(), []
    for (rule, _), ids in sorted(groups.items(), key=lambda g: (g[0][0], -len(g[1]), g[0][1])):
        members = frozenset(ids)
        if len(members) > 1 and members not in seen:
            seen.add(members)
            result.append((rule, sorted(members, key=lambda i: -lookup[i]['occurrences'])))
    return result


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    path.chmod(0o600)


def review_reasons(key):
    """{member id: reason} for reviewed merges, {'apart:<id>|<id>': reason} for kept-separate pairs
    (ids sorted as strings) and {'attr:<bare id>:<owner id>': reason} for
    reviewed per-document attributions; the config texts quote real names, so they only
    reach the viewer encrypted (shown in the node's "merged / separated" panel)."""
    view = lambda i: int(i[3:]) if isinstance(i, str) and i.startswith('kg:') else i
    reasons = {}
    merges = json.loads((ROOT / 'config/identity_merges.json').read_text())
    for item in merges.get('merges', []):
        for member in item['members']:
            reasons[str(member)] = f"{item['confidence']}: {item['reason']}"
    for item in merges.get('kept_separate', []):  # keyed per pair: a node can be kept apart from several
        for a in item['ids']:
            for b in item['ids']:
                if a != b:
                    reasons['apart:' + '|'.join(sorted((str(view(a)), str(view(b)))))] = item['reason']
    for item, owner in attribution_items(key):
        k = f"attr:{view(item['member'])}:{view(owner)}"
        reasons[k] = (reasons[k] + ' | ' if k in reasons else '') + f"{item['confidence']} ({len(item['eftas'])} docs): {item['reason']}"
    return reasons


def write_lookup(key):
    """contact_lookup.json, read by company, entity and document steps. Written
    only when its contents change, so an unchanged lookup triggers no rebuild."""
    lookup = build_lookup(key)
    from ocr_consensus import Consensus  # scan-backed spellings for the reveal only
    scan_spellings = Consensus()
    for entry in lookup.values():
        fixed = scan_spellings.correct(entry['value'])
        if entry.get('kind') == 'participant' or (entry.get('kind') == 'email'
                                                  and re.fullmatch(r'[^@\s]+@[^@\s]+\.[a-z]{2,}', fixed, re.I)):
            entry['value'] = fixed  # an email only ever becomes another address
    path, text = PRIVATE / 'contact_lookup.json', json.dumps(lookup, indent=1, ensure_ascii=False) + '\n'
    if not path.exists() or path.read_text() != text:
        write_private(path, text)
    return lookup


def main():
    key = contact_key()
    lookup = write_lookup(key)
    if '--lookup-only' in sys.argv:  # build.py runs this before the steps that read the lookup
        return
    entity_lookup = PRIVATE / 'entity_contact_lookup.json'
    if entity_lookup.exists():
        lookup.update(json.loads(entity_lookup.read_text()))
    fulltext = PRIVATE / 'fulltext_contact_lookup.json'
    if fulltext.exists():
        for i, entry in json.loads(fulltext.read_text()).items():
            lookup.setdefault(i, {**entry, 'occurrences': 0, 'raw_spellings': {}})
    groups = duplicate_groups(lookup)
    graph_ids = {e['id'] for e in json.loads((ROOT / 'output/graph_entities.json').read_text())}
    rows = [['group', 'rule', 'contact_id', 'value', 'occurrences', 'in_viewer', 'raw_spellings']]
    possible = defaultdict(set)
    for number, (rule, ids) in enumerate(groups, 1):
        for identity in ids:
            entry = lookup[identity]
            rows.append([number, rule, identity, entry['value'], entry['occurrences'], identity in graph_ids, ' | '.join(entry['raw_spellings'])])
            possible[identity].update(lookup[i]['value'] for i in ids if i != identity)
    with open(PRIVATE / 'contact_duplicates.csv', 'w', newline='') as f:
        csv.writer(f).writerows(rows)
    (PRIVATE / 'contact_duplicates.csv').chmod(0o600)
    reveal = {i: {'value': e['value'], 'kind': e['kind'], 'possible_duplicates': sorted(possible.get(i, ())),
                  **({'fields': e['private_fields']} if e.get('private_fields') else {})} for i, e in lookup.items()}
    identity_map = ROOT / 'output/identity_map.json'
    for i, label in fuller_labels(lookup, json.loads(identity_map.read_text()) if identity_map.exists() else {}).items():
        reveal[i]['value'] = label  # merged node shown by its fullest spelling, not a truncated one
    protected = json.loads((ROOT / 'config/survivors.json').read_text())['names']
    for i, repair in cleaned_header_labels(lookup, protected).items():
        reveal[i]['value'] = repair['value']
    addresses = PRIVATE / 'address_lookup.json'  # street addresses from email bodies (build_email_places.py)
    address_values = json.loads(addresses.read_text()) if addresses.exists() else {}
    reveal.update({i: {'value': a['value'] + (f" [signature: {a['signature_block']}]" if a.get('signature_block') else ''), 'possible_duplicates': []}
                   for i, a in address_values.items()})
    # Address pages are titled and searched by the real address too.
    doc_pages = {} if '--no-pages' in sys.argv else write_pages({**lookup, **{i: {'value': a['value']} for i, a in address_values.items()}}, PRIVATE / 'node_docs')
    write_secret(reveal, doc_pages, review_reasons(key))
    (PRIVATE / 'graph_private.html').unlink(missing_ok=True)  # superseded by the unlockable output/graph.html
    by_rule = Counter(rule for rule, _ in groups)
    print(json.dumps({'revealed_by_kind': Counter(e['kind'] for e in lookup.values()), 'duplicate_groups': len(groups), 'groups_by_rule': by_rule,
                      'groups_touching_viewer': sum(any(i in graph_ids for i in ids) for _, ids in groups),
                      'files': [str(PRIVATE / n) for n in ('contact_lookup.json', 'contact_duplicates.csv', 'viewer_secret.json')],
                      'warning': 'PRIVATE: real contact details. Keep local; do not share or commit.'}, indent=2))


if __name__ == '__main__':
    main()
